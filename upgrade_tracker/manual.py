"""工程师补录离线公告（邮件转发、U 盘资料、纸质通知等）。

补录公告与抓取公告走同一套正文版本/附件/适用范围表，
区别仅在 manual_entry=1 且记录 created_by，证据链同样可追溯。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import add_pending
from .hashing import digest
from .ingest import ingest_advisory
from .models import AdvisoryRecord, AdvisoryTarget, Artifact
from .timeutil import now_iso


def record_manual_advisory(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    external_key: str,
    title: str,
    body_text: str,
    targets: list[tuple[str, str, list[str]]],
    severity: str | None = None,
    published_at: str | None = None,
    artifacts: list[Artifact] | None = None,
    attachment_paths: list[str] | None = None,
) -> str:
    """登记一条离线公告并对本地附件计算实际哈希。

    attachment_paths 指向工程师按安全规程单独存放的附件目录；
    本函数只读取并哈希，不执行附件内容。
    """
    if not targets:
        raise ValueError("补录公告至少需要一条适用范围")
    record = AdvisoryRecord(
        external_key=external_key,
        title=title,
        severity=severity,
        published_at=published_at,
        body_text=body_text,
        content_url=None,
        withdrawn=False,
        targets=tuple(
            AdvisoryTarget(vendor=v, product=p, raw_builds=tuple(builds))
            for v, p, builds in targets
        ),
        artifacts=tuple(artifacts or ()),
    )
    announcement_id = ingest_advisory(
        conn, source_id=None, rec=record, manual=True, actor_id=actor_id
    )

    for path in attachment_paths or []:
        file_path = Path(path)
        data = file_path.read_bytes()
        from .db import insert, new_id

        revision_id = conn.execute(
            "SELECT current_revision_id FROM announcements WHERE id = ?",
            (announcement_id,),
        ).fetchone()["current_revision_id"]
        insert(conn, "announcement_artifacts", {
            "id": f"aart_{new_id('x')}", "revision_id": revision_id,
            "file_name": file_path.name, "download_url": None,
            "size_bytes": len(data), "checksum_algorithm": "sha256",
            "checksum_value": None, "observed_sha256": digest(data),
            "first_seen_at": now_iso(),
        })

    add_pending(
        conn, "manual_review",
        f"工程师补录离线公告 {external_key}，请复核适用范围与附件哈希",
        ref_type="announcement", ref_id=announcement_id,
        detail={"actor_id": actor_id},
    )
    conn.commit()
    return announcement_id
