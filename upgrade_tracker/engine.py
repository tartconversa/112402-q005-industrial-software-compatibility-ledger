"""升级建议引擎与升级计划状态机。

对每个 (设备, 发布) 组合，结合以下输入给出三态结论：
  1. 公告适用范围与证据；2. 设备当前构建与身份确认；
  3. 兼容矩阵审核；4. 仍有效的生产例外；5. 允许停机窗口；6. 发布撤回状态。

纪律：
- 版本相近、文件名相同都不构成等价，全部以确认的 build_id / 哈希证据为准；
- 输入哈希未变化时不重算，变化时旧建议进历史、新建议追加；
- 已执行（存在确认回执）的计划永不重算、不取消，只追加新的计划行。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db as dbmod
from .approvals import active_exception, current_compatibility, next_open_window
from .db import dumps_json, get, insert, loads_json, new_id, query, query_one
from .hashing import sha256_text
from .timeutil import now_iso, today

ACTIONABLE = "actionable"
NEEDS_REVIEW = "needs_review"
NOT_APPLICABLE = "not_applicable"

# 原因码（CLI/前端按码本地化展示）
R_DEVICE_IDENTITY = "device_identity_unconfirmed"
R_BUILD_IDENTITY = "build_identity_unconfirmed"
R_RELEASE_IDENTITY = "release_identity_unconfirmed"
R_REJECTED_IDENTITY = "identity_rejected"
R_ALREADY_CURRENT = "already_on_target_build"
R_WITHDRAWN = "release_withdrawn"
R_INCOMPATIBLE = "marked_incompatible"
R_COMPAT_PENDING = "compatibility_pending"
R_ARTIFACT_MISSING = "artifact_evidence_missing"
R_EXCEPTION_ACTIVE = "production_exception_active"
R_NO_WINDOW = "no_maintenance_window"
R_ADVISORY_MISSING = "no_announcement_evidence"
R_ADVISORY_WITHDRAWN = "announcement_evidence_withdrawn"
R_SUPERSEDED = "superseded_by_newer_recommendation"


@dataclass
class DeviceReleaseContext:
    device: sqlite3.Row
    release: sqlite3.Row
    build: sqlite3.Row | None
    product: sqlite3.Row | None
    announcements: list[sqlite3.Row] = field(default_factory=list)


def _announcements_for(conn: sqlite3.Connection, device: sqlite3.Row,
                       release: sqlite3.Row) -> list[sqlite3.Row]:
    """找出适用该设备产品、且覆盖该发布构建的公告（含撤回公告）。"""
    build = get(conn, "builds", release["build_id"]) if release["build_id"] else None
    if build is None:
        return []
    targets = query(
        conn,
        "SELECT DISTINCT announcement_id FROM announcement_targets WHERE product_id = ?",
        (build["product_id"],),
    )
    result: list[sqlite3.Row] = []
    for row in targets:
        adv = get(conn, "announcements", row["announcement_id"])
        if adv is None:
            continue
        rev = get(conn, "announcement_revisions", adv["current_revision_id"])
        if rev is None:
            continue
        target = query_one(
            conn,
            """SELECT * FROM announcement_targets WHERE revision_id = ?
               AND (product_id = ? OR (product_id IS NULL AND product_hint = ?))""",
            (rev["id"], build["product_id"], release["product_hint"]),
        )
        if target is None:
            continue
        raw_builds = loads_json(target["raw_builds"], [])
        # 适用范围只给原文构建列表；仅当发布构建号被显式列入才算命中，
        # 不做“版本相近”推断。空列表表示按产品整体适用。
        if not raw_builds or build["build_number"] in raw_builds or build["version"] in raw_builds:
            result.append(adv)
    return result


def _identity_state(conn: sqlite3.Connection, device: sqlite3.Row,
                    release: sqlite3.Row) -> tuple[str | None, list[str]]:
    """返回 (状态, 原因码列表)。状态为 None 表示身份闸门通过。"""
    reasons: list[str] = []
    if device["product_id"] is None or device["current_build_id"] is None:
        identity = query_one(
            conn,
            "SELECT status FROM build_identities WHERE product_hint = ? AND build_raw = ?",
            (device["product_hint"], device["current_build_raw"]),
        )
        reasons.append(R_REJECTED_IDENTITY if identity and identity["status"] == "rejected"
                       else R_DEVICE_IDENTITY)
    if release["build_id"] is None:
        reasons.append(R_RELEASE_IDENTITY)
    else:
        build = get(conn, "builds", release["build_id"])
        identity = query_one(
            conn,
            "SELECT status FROM build_identities WHERE product_hint = ? AND build_raw = ?",
            (release["product_hint"], build["build_number"] if build else ""),
        )
        if identity is None or identity["status"] != "confirmed":
            reasons.append(
                R_REJECTED_IDENTITY if identity and identity["status"] == "rejected"
                else R_BUILD_IDENTITY
            )
    return (R_REJECTED_IDENTITY if R_REJECTED_IDENTITY in reasons else None), reasons


def evaluate(conn: sqlite3.Connection, device: sqlite3.Row,
             release: sqlite3.Row, on_date=None) -> tuple[str, list[str], list[str]]:
    """评估单个 (设备, 发布)，返回 (状态, 原因码, 公告id列表)。"""
    on_date = on_date or today()
    announcements = _announcements_for(conn, device, release)
    adv_ids = [a["id"] for a in announcements]
    hard_block, reasons = _identity_state(conn, device, release)

    # 不适用类判定（即便身份未确认，撤回/同构建仍可直接判定的情形分开处理）
    if release["state"] in ("withdrawn", "yanked"):
        reasons.append(R_WITHDRAWN)
        return NOT_APPLICABLE, reasons, adv_ids

    if hard_block == R_REJECTED_IDENTITY:
        return NEEDS_REVIEW, reasons, adv_ids

    if R_DEVICE_IDENTITY in reasons or R_RELEASE_IDENTITY in reasons \
            or R_BUILD_IDENTITY in reasons:
        # 身份未确认：其余闸门没有可信输入，直接转人工复核，
        # 绝不能因为版本号/构建号文本相同就判“已是目标构建”。
        if not announcements:
            reasons.append(R_ADVISORY_MISSING)
        return NEEDS_REVIEW, reasons, adv_ids

    if (device["current_build_id"] and release["build_id"]
            and device["current_build_id"] == release["build_id"]):
        reasons.append(R_ALREADY_CURRENT)
        return NOT_APPLICABLE, reasons, adv_ids

    build = get(conn, "builds", release["build_id"])
    product_id = build["product_id"]

    compat = current_compatibility(conn, release["id"], product_id)
    if compat is None or compat["decision"] == "pending":
        reasons.append(R_COMPAT_PENDING)
        return NEEDS_REVIEW, reasons, adv_ids
    if compat["decision"] == "incompatible":
        reasons.append(R_INCOMPATIBLE)
        return NOT_APPLICABLE, reasons, adv_ids

    # 附件证据：至少要有一个登记附件（校验值可在下载后补全）
    artifact = query_one(
        conn, "SELECT id FROM release_artifacts WHERE release_id = ?", (release["id"],)
    )
    if artifact is None:
        reasons.append(R_ARTIFACT_MISSING)
        return NEEDS_REVIEW, reasons, adv_ids

    if not announcements:
        reasons.append(R_ADVISORY_MISSING)
        return NEEDS_REVIEW, reasons, adv_ids

    # 证据公告被厂商撤回：不能再作为升级依据，转人工复核
    if any(a["withdrawn"] for a in announcements):
        reasons.append(R_ADVISORY_WITHDRAWN)
        return NEEDS_REVIEW, reasons, adv_ids

    # 仍有效的生产例外：暂缓期间不能排产，但结论仍是“可执行、暂缓中”
    exception = active_exception(conn, device["id"], on_date)
    if exception is not None:
        reasons.append(f"{R_EXCEPTION_ACTIVE}:{exception['id']}")

    window = next_open_window(conn, device["id"], on_date)
    if window is None:
        reasons.append(R_NO_WINDOW)

    return ACTIONABLE, reasons, adv_ids


def _inputs_hash(conn: sqlite3.Connection, device_id: str,
                 release_id: str, reasons: list[str], adv_ids: list[str],
                 on_date=None) -> str:
    """对影响结论的全部输入做哈希；任何一项变化都会触发重算。

    评估日期也参与哈希：例外到期、窗口到来只取决于日期推进，
    即使库中数据未变，跨天后也必须重算。
    """
    on_date = on_date or today()
    device = get(conn, "devices", device_id)
    release = get(conn, "releases", release_id)
    compat = current_compatibility(conn, release_id,
                                   get(conn, "builds", release["build_id"])["product_id"]
                                   if release["build_id"] else "")
    parts = [
        on_date.isoformat(),
        device["current_build_raw"], device["current_build_id"],
        release["state"], release["build_id"],
        compat["decision"] if compat else "none",
        dumps_json(reasons), dumps_json(sorted(adv_ids)),
    ]
    exc = conn.execute(
        "SELECT id, state, ends_on FROM production_exceptions WHERE device_id = ? ORDER BY id",
        (device_id,),
    ).fetchall()
    parts.append(dumps_json([dict(r) for r in exc]))
    wins = conn.execute(
        "SELECT id, starts_on, ends_on FROM maintenance_windows WHERE device_id = ? ORDER BY id",
        (device_id,),
    ).fetchall()
    parts.append(dumps_json([dict(r) for r in wins]))
    return sha256_text("|".join(str(p) for p in parts))


def recompute_device(conn: sqlite3.Connection, device_id: str,
                     on_date=None) -> list[sqlite3.Row]:
    """重算一台设备对所有在库发布的建议，并同步升级计划。"""
    device = get(conn, "devices", device_id)
    if device is None:
        raise ValueError(f"设备不存在: {device_id}")
    releases = query(conn, "SELECT * FROM releases ORDER BY posted_at DESC, id")
    current_rows: list[sqlite3.Row] = []
    for release in releases:
        status, reasons, adv_ids = evaluate(conn, device, release, on_date=on_date)
        inputs = _inputs_hash(conn, device_id, release["id"], reasons,
                              adv_ids, on_date)
        existing = query_one(
            conn,
            """SELECT * FROM recommendations r
               WHERE r.device_id = ? AND r.release_id = ?
                 AND r.id NOT IN (
                     SELECT supersedes_id FROM recommendations
                     WHERE supersedes_id IS NOT NULL
                 )""",
            (device_id, release["id"]),
        )
        if existing and existing["inputs_hash"] == inputs:
            current_rows.append(existing)
            continue

        rec_id = f"rec_{new_id('x')}"
        ts = now_iso()
        insert(conn, "recommendations", {
            "id": rec_id, "device_id": device_id, "release_id": release["id"],
            "status": status, "reasons_json": dumps_json(reasons),
            "linked_announcement_ids": dumps_json(adv_ids),
            "inputs_hash": inputs, "computed_at": ts,
            "supersedes_id": existing["id"] if existing else None,
        })
        insert(conn, "recommendation_history", {
            "id": f"rhist_{new_id('x')}", "device_id": device_id,
            "release_id": release["id"], "status": status,
            "reasons_json": dumps_json(reasons),
            "linked_announcement_ids": dumps_json(adv_ids),
            "inputs_hash": inputs, "computed_at": ts,
        })
        # 旧建议保留完整行，新行通过 supersedes_id 指向它；
        # “当前建议”始终按“未被任何行 supersedes”查询。
        row = get(conn, "recommendations", rec_id)
        current_rows.append(row)  # type: ignore[arg-type]
        _sync_plan(conn, row, on_date=on_date)
    conn.commit()
    return current_rows


def recompute_all(conn: sqlite3.Connection, on_date=None) -> int:
    """重算全部设备，返回处理的设备数。"""
    devices = query(conn, "SELECT id FROM devices ORDER BY id")
    for row in devices:
        recompute_device(conn, row["id"], on_date=on_date)
    return len(devices)


def _sync_plan(conn: sqlite3.Connection, recommendation: sqlite3.Row,
               on_date=None) -> None:
    """根据最新建议维护升级计划；已执行计划冻结，不再改动。"""
    on_date = on_date or today()
    device_id = recommendation["device_id"]
    release_id = recommendation["release_id"]

    frozen = query_one(
        conn,
        """SELECT up.* FROM upgrade_plans up
           JOIN execution_receipts er ON er.plan_id = up.id
           WHERE up.device_id = ? AND up.release_id = ?
             AND er.confirmed_at IS NOT NULL""",
        (device_id, release_id),
    )
    if frozen is not None:
        return  # 已执行设备保留当时版本与批准记录，重算不触碰

    plan = query_one(
        conn,
        """SELECT * FROM upgrade_plans
           WHERE device_id = ? AND release_id = ?
             AND state IN ('blocked', 'scheduled', 'in_progress')""",
        (device_id, release_id),
    )

    if recommendation["status"] == NOT_APPLICABLE:
        if plan is not None and plan["state"] != "in_progress":
            # 取消时清掉上一轮的阻塞原因，避免展示过时文本
            conn.execute(
                "UPDATE upgrade_plans SET blocked_reason = NULL, updated_at = ? WHERE id = ?",
                (now_iso(), plan["id"]),
            )
            _transition(conn, plan, "canceled", reason_code=R_SUPERSEDED,
                        note="建议变为不适用，计划取消")
        return

    reasons = loads_json(recommendation["reasons_json"], [])
    exception_code = next((r for r in reasons if r.startswith(R_EXCEPTION_ACTIVE)), None)
    no_window = R_NO_WINDOW in reasons
    window = next_open_window(conn, device_id, on_date)

    if recommendation["status"] == NEEDS_REVIEW:
        blocked = "; ".join(r for r in reasons if not r.startswith(R_EXCEPTION_ACTIVE))
        _upsert_plan(conn, recommendation, "blocked", None, blocked or "需人工复核", plan)
        return

    # actionable：有窗口且无生效例外才可排期，否则阻塞并写明原因
    if exception_code:
        exception_id = exception_code.split(":", 1)[1]
        row = get(conn, "production_exceptions", exception_id)
        _upsert_plan(conn, recommendation, "blocked", None,
                     f"生产例外生效中（至 {row['ends_on']}）：{row['reason']}", plan)
    elif no_window or window is None:
        _upsert_plan(conn, recommendation, "blocked", None,
                     "尚无允许停机窗口", plan)
    else:
        _upsert_plan(conn, recommendation, "scheduled", window["id"], None, plan)


def _upsert_plan(conn: sqlite3.Connection, recommendation: sqlite3.Row,
                 state: str, window_id: str | None, blocked_reason: str | None,
                 existing: sqlite3.Row | None) -> None:
    ts = now_iso()
    if existing is None:
        plan_id = f"plan_{new_id('x')}"
        insert(conn, "upgrade_plans", {
            "id": plan_id, "recommendation_id": recommendation["id"],
            "device_id": recommendation["device_id"],
            "release_id": recommendation["release_id"], "state": state,
            "window_id": window_id, "blocked_reason": blocked_reason,
            "created_at": ts, "updated_at": ts,
        })
        _log_event(conn, plan_id, "created", None, state, note=blocked_reason)
        return
    if existing["state"] == "in_progress":
        # 执行中的计划保持状态，只更新阻塞信息与窗口，避免现场作业被静默改期
        conn.execute(
            "UPDATE upgrade_plans SET blocked_reason = ?, window_id = COALESCE(?, window_id), updated_at = ? WHERE id = ?",
            (blocked_reason, window_id, ts, existing["id"]),
        )
        return
    if existing["state"] != state or existing["window_id"] != window_id \
            or existing["blocked_reason"] != blocked_reason:
        old_state = existing["state"]
        conn.execute(
            """UPDATE upgrade_plans SET state = ?, window_id = ?, blocked_reason = ?,
                   recommendation_id = ?, updated_at = ? WHERE id = ?""",
            (state, window_id, blocked_reason, recommendation["id"],
             ts, existing["id"]),
        )
        _log_event(conn, existing["id"], "recalculated", old_state, state,
                   note=blocked_reason)


def _transition(conn: sqlite3.Connection, plan: sqlite3.Row, new_state: str,
                *, reason_code: str | None = None, note: str | None = None,
                actor_id: str | None = None) -> None:
    old = plan["state"]
    conn.execute(
        "UPDATE upgrade_plans SET state = ?, updated_at = ? WHERE id = ?",
        (new_state, now_iso(), plan["id"]),
    )
    _log_event(conn, plan["id"], reason_code or "transition", old, new_state,
               actor_id=actor_id, note=note)


def _log_event(conn: sqlite3.Connection, plan_id: str, event_type: str,
               from_state: str | None, to_state: str,
               *, actor_id: str | None = None, note: str | None = None) -> None:
    insert(conn, "upgrade_plan_events", {
        "id": f"pev_{new_id('x')}", "plan_id": plan_id,
        "event_type": event_type, "from_state": from_state,
        "to_state": to_state, "actor_id": actor_id, "note": note,
        "occurred_at": now_iso(),
    })
