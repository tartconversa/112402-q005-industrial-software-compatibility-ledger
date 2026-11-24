"""采集编排：条件请求、原始快照、失败入队、解析与落库。

关键纪律：任何一次抓取失败、格式变化或“成功但为空”都会产生待处理项，
空结果永远不会被当成“没有新版本”。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db as dbmod
from .hashing import digest
from .ingest import ingest_payload
from .models import EmptySuspectError, ParseError
from .parsers import parse
from .timeutil import now_iso
from .transport import FetchResult, HttpTransport, Transport


@dataclass
class FetchSummary:
    source_id: str
    outcome: str
    pending_id: str | None
    detail: str


def register_source(conn: sqlite3.Connection, *, source_id: str, display_name: str,
                    kind: str, url: str, parser: str,
                    credentials: str | None = None) -> None:
    """登记或更新一个外部来源。"""
    ts = now_iso()
    existing = dbmod.get(conn, "sources", source_id)
    if existing is None:
        dbmod.insert(conn, "sources", {
            "id": source_id, "display_name": display_name, "kind": kind,
            "url": url, "parser": parser, "credentials": credentials,
            "enabled": 1, "etag": None, "last_modified": None,
            "config_json": "{}", "created_at": ts, "updated_at": ts,
        })
    else:
        conn.execute(
            """UPDATE sources SET display_name = ?, kind = ?, url = ?, parser = ?,
                   credentials = COALESCE(?, credentials), updated_at = ?
               WHERE id = ?""",
            (display_name, kind, url, parser, credentials, ts, source_id),
        )


def fetch_one(conn: sqlite3.Connection, source_id: str,
              transport: Transport | None = None) -> FetchSummary:
    """抓取并处理单个来源，返回结构化结果（任何异常都不吞掉为“无更新”）。"""
    transport = transport or HttpTransport()
    source = dbmod.get(conn, "sources", source_id)
    if source is None:
        raise ValueError(f"来源不存在: {source_id}")
    if not source["enabled"]:
        return FetchSummary(source_id, "disabled", None, "来源已停用")

    started = now_iso()
    run_id = dbmod.new_id("run")
    dbmod.insert(conn, "fetch_runs", {
        "id": run_id, "source_id": source_id, "started_at": started,
        "finished_at": None, "outcome": "http_error",
        "http_status": None, "error_message": None,
        "bytes_received": None, "content_sha256": None, "raw_snapshot_id": None,
    })

    result = transport.get(
        source["url"], etag=source["etag"],
        last_modified=source["last_modified"],
        credentials=source["credentials"],
    )

    if result.not_modified:
        _finish_run(conn, run_id, "not_modified", 304, None, None, None, None)
        conn.execute(
            "UPDATE sources SET updated_at = ? WHERE id = ?", (now_iso(), source_id)
        )
        conn.commit()
        return FetchSummary(source_id, "not_modified", None, "内容未变化（304）")

    if not result.ok:
        outcome = "http_error" if result.status else "network_error"
        _finish_run(conn, run_id, outcome, result.status or None,
                    result.error_message, None, None, None)
        pending_id = dbmod.add_pending(
            conn, "fetch_failure",
            f"来源 {source_id} 抓取失败：{result.error_message}",
            source_id=source_id, fetch_run_id=run_id,
            detail={"status": result.status},
        )
        conn.commit()
        return FetchSummary(source_id, outcome, pending_id,
                            result.error_message or "抓取失败")

    snapshot_id = _store_snapshot(conn, source_id, run_id, result)
    _finish_run(conn, run_id, "ok", result.status, None,
                len(result.body), digest(result.body), snapshot_id)
    conn.execute(
        "UPDATE sources SET etag = ?, last_modified = ?, updated_at = ? WHERE id = ?",
        (result.etag, result.last_modified, now_iso(), source_id),
    )

    try:
        payload = parse(source["parser"], result.body)
    except EmptySuspectError as exc:
        conn.execute(
            "UPDATE fetch_runs SET outcome = 'empty_suspect' WHERE id = ?", (run_id,)
        )
        pending_id = dbmod.add_pending(
            conn, "empty_suspect",
            f"来源 {source_id} 返回空结果，禁止视为无新版本：{exc}",
            source_id=source_id, fetch_run_id=run_id,
            detail={"snapshot_id": snapshot_id},
        )
        conn.commit()
        return FetchSummary(source_id, "empty_suspect", pending_id, str(exc))
    except ParseError as exc:
        conn.execute(
            "UPDATE fetch_runs SET outcome = 'parse_error' WHERE id = ?", (run_id,)
        )
        pending_id = dbmod.add_pending(
            conn, "parse_failure",
            f"来源 {source_id} 页面格式变化或内容非法：{exc}",
            source_id=source_id, fetch_run_id=run_id,
            detail={"snapshot_id": snapshot_id},
        )
        conn.commit()
        return FetchSummary(source_id, "parse_error", pending_id, str(exc))

    ingest_payload(conn, source_id, payload, snapshot_id)
    conn.commit()
    return FetchSummary(source_id, "ok", None, f"快照 {snapshot_id} 已入库")


def fetch_all(conn: sqlite3.Connection,
              transport: Transport | None = None) -> list[FetchSummary]:
    """抓取所有启用来源，单个失败不影响其他来源。"""
    sources = dbmod.query(conn, "SELECT id FROM sources WHERE enabled = 1 ORDER BY id")
    summaries: list[FetchSummary] = []
    for row in sources:
        try:
            summaries.append(fetch_one(conn, row["id"], transport))
        except Exception as exc:  # noqa: BLE001 - 调度器不能因单源异常整体退出
            pending_id = dbmod.add_pending(
                conn, "fetch_failure",
                f"来源 {row['id']} 抓取过程异常：{exc}",
                source_id=row["id"],
            )
            conn.commit()
            summaries.append(FetchSummary(row["id"], "network_error",
                                          pending_id, str(exc)))
    return summaries


def _store_snapshot(conn: sqlite3.Connection, source_id: str, run_id: str,
                    result: FetchResult) -> str:
    snapshot_id = dbmod.new_id("snap")
    dbmod.insert(conn, "raw_snapshots", {
        "id": snapshot_id, "source_id": source_id, "fetched_at": now_iso(),
        "content_type": result.content_type, "body": result.body,
        "sha256": digest(result.body), "size_bytes": len(result.body),
        "etag": result.etag, "last_modified": result.last_modified,
    })
    conn.execute(
        "UPDATE fetch_runs SET raw_snapshot_id = ? WHERE id = ?",
        (snapshot_id, run_id),
    )
    return snapshot_id


def _finish_run(conn: sqlite3.Connection, run_id: str, outcome: str,
                http_status: int | None, error_message: str | None,
                bytes_received: int | None, content_sha256: str | None,
                snapshot_id: str | None) -> None:
    conn.execute(
        """UPDATE fetch_runs SET finished_at = ?, outcome = ?, http_status = ?,
               error_message = ?, bytes_received = ?, content_sha256 = ?,
               raw_snapshot_id = ?
           WHERE id = ?""",
        (now_iso(), outcome, http_status, error_message, bytes_received,
         content_sha256, snapshot_id, run_id),
    )
