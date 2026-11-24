"""把解析后的规范化负载落库（增量、可追溯、不覆盖证据）。"""
from __future__ import annotations

import sqlite3

from .db import add_pending, dumps_json, insert, new_id, query, query_one
from .hashing import sha256_text
from .identity import (
    ensure_build,
    ensure_identity_row,
    ensure_product,
    ensure_vendor,
    resolve_identity,
)
from .models import (
    AdvisoryPayload,
    AdvisoryRecord,
    Artifact,
    CatalogPayload,
    DeviceRecord,
    InventoryPayload,
    ReleaseRecord,
)
from .timeutil import now_iso


# ---------------- 发布目录 ----------------

def _release_pk(source_id: str, external_id: str) -> str:
    return f"rel_{source_id}_{external_id}"


def ingest_releases(conn: sqlite3.Connection, source_id: str,
                    records: tuple[ReleaseRecord, ...]) -> None:
    """增量写入发布目录；本次未出现的既有发布标记为 withdrawn。"""
    seen_pks: set[str] = set()
    for rec in records:
        pk = _release_pk(source_id, rec.external_id)
        seen_pks.add(pk)
        vendor_id = ensure_vendor(conn, rec.vendor)
        product_id = ensure_product(conn, vendor_id, rec.product)
        identity = resolve_identity(conn, rec.vendor, rec.product, rec.build_number)
        if identity is not None:
            build_id = identity["build_id"]
        else:
            build_id = ensure_build(conn, product_id, rec.version, rec.build_number)
            ensure_identity_row(conn, rec.vendor, rec.product, rec.build_number)

        existing = query_one(conn, "SELECT * FROM releases WHERE source_id = ? AND id = ?",
                             (source_id, pk))
        ts = now_iso()
        if existing is None:
            insert(conn, "releases", {
                "id": pk, "source_id": source_id, "build_id": build_id,
                "product_hint": rec.product, "vendor_hint": rec.vendor,
                "channel": rec.channel, "posted_at": rec.posted_at,
                "state": rec.state, "first_seen_at": ts, "last_seen_at": ts,
                "state_changed_at": ts if rec.state != "active" else None,
            })
        else:
            changed = existing["state"] != rec.state
            conn.execute(
                """UPDATE releases SET build_id = COALESCE(build_id, ?),
                       state = ?, last_seen_at = ?,
                       state_changed_at = CASE WHEN ? THEN ? ELSE state_changed_at END
                   WHERE id = ?""",
                (build_id, rec.state, ts, int(changed), ts, pk),
            )
        _upsert_artifacts(conn, pk, rec.artifacts)

    # 目录里消失的发布：厂商撤回或页面变动，标记 withdrawn 并要求复核
    for row in query(
        conn,
        "SELECT id, state FROM releases WHERE source_id = ? AND id NOT IN (%s)"
        % ",".join("?" for _ in seen_pks) if seen_pks else
        "SELECT id, state FROM releases WHERE source_id = ? AND 0",
        (source_id, *seen_pks) if seen_pks else (source_id,),
    ):
        if row["state"] != "withdrawn":
            conn.execute(
                "UPDATE releases SET state = 'withdrawn', state_changed_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
            add_pending(
                conn, "manual_review",
                f"发布 {row['id']} 在最新目录中消失，已按撤回处理，请人工核实",
                source_id=source_id, ref_type="release", ref_id=row["id"],
            )


def _upsert_artifacts(conn: sqlite3.Connection, release_id: str,
                      artifacts: tuple[Artifact, ...]) -> None:
    ts = now_iso()
    for art in artifacts:
        existing = query_one(
            conn,
            "SELECT id FROM release_artifacts WHERE release_id = ? AND file_name = ? AND download_url = ?",
            (release_id, art.file_name, art.download_url),
        )
        if existing:
            continue
        insert(conn, "release_artifacts", {
            "id": f"art_{new_id('x')}", "release_id": release_id,
            "file_name": art.file_name, "download_url": art.download_url,
            "size_bytes": art.size_bytes,
            "checksum_algorithm": art.checksum_algorithm,
            "checksum_value": art.checksum_value,
            "observed_sha256": None, "first_seen_at": ts,
        })


# ---------------- 公告 ----------------

def _announcement_pk(source_id: str | None, external_key: str) -> str:
    return f"adv_{source_id or 'manual'}_{external_key}"


def ingest_advisory(conn: sqlite3.Connection, source_id: str | None,
                    rec: AdvisoryRecord, *, manual: bool = False,
                    actor_id: str | None = None,
                    fetch_run_id: str | None = None) -> str:
    """写入/更新一条公告；正文变化时追加 revision，不覆盖旧正文。"""
    pk = _announcement_pk(source_id, rec.external_key)
    ts = now_iso()
    body_hash = sha256_text(rec.body_text)
    existing = query_one(conn, "SELECT * FROM announcements WHERE id = ?", (pk,))

    if existing is None:
        insert(conn, "announcements", {
            "id": pk, "source_id": source_id,
            "manual_entry": 1 if manual else 0,
            "external_key": rec.external_key, "title": rec.title,
            "severity": rec.severity, "published_at": rec.published_at,
            "withdrawn": 1 if rec.withdrawn else 0,
            "withdrawn_at": ts if rec.withdrawn else None,
            "current_revision_id": None, "created_at": ts,
        })
        next_rev = 1
    else:
        latest = query_one(
            conn,
            "SELECT MAX(revision_no) AS rev FROM announcement_revisions WHERE announcement_id = ?",
            (pk,),
        )
        next_rev = (latest["rev"] or 0) + 1
        if existing["withdrawn"] != (1 if rec.withdrawn else 0):
            conn.execute(
                "UPDATE announcements SET withdrawn = ?, withdrawn_at = ? WHERE id = ?",
                (1 if rec.withdrawn else 0, ts if rec.withdrawn else None, pk),
            )

    # 正文哈希相同则复用当前版本，不制造无意义修订
    same_rev = query_one(
        conn,
        "SELECT id FROM announcement_revisions WHERE announcement_id = ? AND body_sha256 = ?",
        (pk, body_hash),
    )
    if same_rev:
        revision_id = same_rev["id"]
    else:
        revision_id = f"rev_{new_id('x')}"
        insert(conn, "announcement_revisions", {
            "id": revision_id, "announcement_id": pk,
            "revision_no": next_rev, "body_text": rec.body_text,
            "body_sha256": body_hash, "content_url": rec.content_url,
            "fetched_run_id": fetch_run_id, "created_at": ts,
            "created_by": actor_id,
        })
        for art in rec.artifacts:
            insert(conn, "announcement_artifacts", {
                "id": f"aart_{new_id('x')}", "revision_id": revision_id,
                "file_name": art.file_name, "download_url": art.download_url,
                "size_bytes": art.size_bytes,
                "checksum_algorithm": art.checksum_algorithm,
                "checksum_value": art.checksum_value,
                "observed_sha256": None, "first_seen_at": ts,
            })
        _write_targets(conn, pk, revision_id, rec)

    conn.execute("UPDATE announcements SET current_revision_id = ? WHERE id = ?",
                 (revision_id, pk))
    return pk


def _write_targets(conn: sqlite3.Connection, announcement_id: str,
                   revision_id: str, rec: AdvisoryRecord) -> None:
    for target in rec.targets:
        vendor_id = ensure_vendor(conn, target.vendor)
        product = query_one(
            conn, "SELECT id FROM products WHERE vendor_id = ? AND name = ?",
            (vendor_id, target.product),
        )
        product_id = product["id"] if product else None
        if product_id is None:
            new_product_id = ensure_product(conn, vendor_id, target.product)
            product_id = new_product_id
        existing = query_one(
            conn,
            "SELECT id FROM announcement_targets WHERE revision_id = ? AND product_hint = ?",
            (revision_id, target.product),
        )
        if existing:
            continue
        insert(conn, "announcement_targets", {
            "id": f"tgt_{new_id('x')}", "announcement_id": announcement_id,
            "revision_id": revision_id,
            "product_id": product_id,
            "product_hint": target.product,
            "raw_builds": dumps_json(list(target.raw_builds)),
            "vendor_hint": target.vendor,
        })
        # 适用范围中的原文构建也必须经过身份确认
        for build_raw in target.raw_builds:
            ensure_identity_row(conn, target.vendor, target.product, build_raw)


def ingest_advisories(conn: sqlite3.Connection, source_id: str | None,
                      records: tuple[AdvisoryRecord, ...],
                      *, manual: bool = False,
                      actor_id: str | None = None,
                      fetch_run_id: str | None = None) -> list[str]:
    return [
        ingest_advisory(conn, source_id, rec, manual=manual,
                        actor_id=actor_id, fetch_run_id=fetch_run_id)
        for rec in records
    ]


# ---------------- 设备清单 ----------------

def ingest_inventory(conn: sqlite3.Connection, source_id: str,
                     records: tuple[DeviceRecord, ...],
                     raw_snapshot_id: str) -> None:
    """增量同步授权设备；构建变化写 inventory_events 审计事件。"""
    ts = now_iso()
    seen: set[str] = set()
    for rec in records:
        seen.add(rec.device)
        identity = resolve_identity(conn, "", rec.product, rec.build_raw)
        # vendor 提示在设备清单中可能缺失：按产品名跨厂商再查一次
        if identity is None:
            identity = query_one(
                conn,
                """SELECT bi.* FROM build_identities bi
                   WHERE bi.product_hint = ? AND bi.build_raw = ?
                     AND bi.status = 'confirmed'""",
                (rec.product, rec.build_raw),
            )
        product_id = identity["product_id"] if identity else None
        build_id = identity["build_id"] if identity else None
        if identity is None:
            ensure_identity_row(conn, "", rec.product, rec.build_raw)
            # 构建实体只做去重复用，不直接绑定设备——同名同号是否等价必须
            # 经资产管理员确认；设备的 product_id/current_build_id 保持空值，
            # 由 confirm_identity 回填。
            existing_build = query_one(
                conn,
                """SELECT b.* FROM builds b JOIN products p ON p.id = b.product_id
                   WHERE p.name = ? AND b.build_number = ? LIMIT 1""",
                (rec.product, rec.build_raw),
            )
            if existing_build is None:
                existing_product = query_one(
                    conn, "SELECT id FROM products WHERE name = ?", (rec.product,)
                )
                if existing_product:
                    product_id = existing_product["id"]
                else:
                    vendor_id = ensure_vendor(conn, "未登记厂商")
                    product_id = ensure_product(conn, vendor_id, rec.product)
                # 设备清单只给原文构建，版本号与构建号先按原文落，不做拆分推断
                ensure_build(conn, product_id, rec.build_raw, rec.build_raw)
            product_id, build_id = None, None

        existing = query_one(
            conn,
            "SELECT * FROM devices WHERE inventory_source_id = ? AND id = ?",
            (source_id, rec.device),
        )
        if existing is None:
            insert(conn, "devices", {
                "id": rec.device, "name": rec.name, "line_id": rec.line,
                "product_id": product_id, "product_hint": rec.product,
                "current_build_id": build_id,
                "current_build_raw": rec.build_raw,
                "inventory_source_id": source_id, "updated_at": ts,
            })
            insert(conn, "inventory_events", {
                "id": f"iev_{new_id('x')}", "device_id": rec.device,
                "source_id": source_id, "old_build_raw": None,
                "new_build_raw": rec.build_raw,
                "raw_snapshot_id": raw_snapshot_id, "occurred_at": ts,
            })
        else:
            old_build = existing["current_build_raw"]
            conn.execute(
                """UPDATE devices SET name = ?, line_id = ?,
                       product_id = COALESCE(product_id, ?),
                       current_build_id = COALESCE(current_build_id, ?),
                       current_build_raw = ?, updated_at = ?
                   WHERE id = ?""",
                (rec.name, rec.line, product_id, build_id, rec.build_raw, ts,
                 rec.device),
            )
            if old_build != rec.build_raw:
                insert(conn, "inventory_events", {
                    "id": f"iev_{new_id('x')}", "device_id": rec.device,
                    "source_id": source_id, "old_build_raw": old_build,
                    "new_build_raw": rec.build_raw,
                    "raw_snapshot_id": raw_snapshot_id, "occurred_at": ts,
                })

    # 清单中消失的设备不删除（历史可追溯），转人工核实
    if seen:
        placeholders = ",".join("?" for _ in seen)
        missing = query(
            conn,
            f"SELECT id FROM devices WHERE inventory_source_id = ? AND id NOT IN ({placeholders})",
            (source_id, *seen),
        )
        for row in missing:
            add_pending(
                conn, "manual_review",
                f"设备 {row['id']} 不在最新授权清单中，请核实是否退役或来源异常",
                source_id=source_id, ref_type="device", ref_id=row["id"],
            )


def ingest_payload(conn: sqlite3.Connection, source_id: str, payload: object,
                   raw_snapshot_id: str) -> None:
    """按负载类型分派落库。"""
    if isinstance(payload, CatalogPayload):
        ingest_releases(conn, source_id, payload.releases)
        if payload.advisories:
            ingest_advisories(conn, source_id, payload.advisories)
    elif isinstance(payload, AdvisoryPayload):
        ingest_advisories(conn, source_id, payload.advisories)
    elif isinstance(payload, InventoryPayload):
        ingest_inventory(conn, source_id, payload.devices, raw_snapshot_id)
    else:  # pragma: no cover - 解析器注册表已约束类型
        raise TypeError(f"未知负载类型: {type(payload)!r}")
