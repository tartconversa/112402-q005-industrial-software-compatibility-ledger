"""定时调度与待处理队列管理。

调度器按固定间隔执行“抓取全部来源 → 重算建议”。部署时也可以直接
用系统 cron/定时器每次调用 ``upgrade-tracker run-once``，本模块的
循环模式适合容器化常驻。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .db import get, query
from .engine import recompute_all
from .fetch import fetch_all
from .timeutil import now_iso


@dataclass
class CycleReport:
    fetched: int
    failures: int
    pending_open: int
    devices_recomputed: int


def run_once(conn: sqlite3.Connection, transport=None) -> CycleReport:
    """执行一个完整周期；任何单源失败都已转为待处理项，不向上抛。"""
    summaries = fetch_all(conn, transport)
    recompute_all(conn)
    failures = sum(1 for s in summaries if s.outcome not in ("ok", "not_modified", "disabled"))
    pending_open = query(
        conn, "SELECT COUNT(*) AS n FROM pending_queue WHERE status = 'open'"
    )[0]["n"]
    devices = query(conn, "SELECT COUNT(*) AS n FROM devices")[0]["n"]
    return CycleReport(len(summaries), failures, pending_open, devices)


def run_forever(conn: sqlite3.Connection, interval_seconds: int,
                transport=None, *, sleep=time.sleep) -> None:
    """常驻循环，主要给容器部署；测试通过注入 sleep 避免真实等待。"""
    while True:
        run_once(conn, transport)
        sleep(interval_seconds)


def list_pending(conn: sqlite3.Connection, kind: str | None = None,
                 status: str = "open") -> list[sqlite3.Row]:
    sql = "SELECT * FROM pending_queue WHERE status = ?"
    params: tuple = (status,)
    if kind:
        sql += " AND kind = ?"
        params = (status, kind)
    sql += " ORDER BY created_at"
    return query(conn, sql, params)


def resolve_pending(conn: sqlite3.Connection, pending_id: str, actor_id: str,
                    resolution: str) -> None:
    """人工处理完待办后关闭（需写明处理结论）。"""
    row = get(conn, "pending_queue", pending_id)
    if row is None:
        raise ValueError(f"待办不存在: {pending_id}")
    conn.execute(
        """UPDATE pending_queue SET status = 'resolved', resolved_at = ?,
               resolved_by = ?, resolution = ? WHERE id = ?""",
        (now_iso(), actor_id, resolution, pending_id),
    )
    conn.commit()
