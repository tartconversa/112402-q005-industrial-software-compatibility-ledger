"""兼容矩阵、停机窗口与生产例外的登记与确认。

兼容关系由产线工程师确认；纠正旧结论不是覆盖，而是新增一条
supersedes 旧记录的决定，升级重算据此追溯结论变化。
"""
from __future__ import annotations

import sqlite3
from datetime import date

from .db import add_pending, get, insert, new_id, query_one
from .timeutil import now_iso, parse_date


# ---------------- 兼容矩阵 ----------------

def set_compatibility(conn: sqlite3.Connection, *, release_id: str,
                      product_id: str, decision: str, actor_id: str,
                      note: str | None = None) -> str:
    """登记兼容决定；对同一 (发布, 产品) 的再次登记视为纠正。"""
    if decision not in ("compatible", "incompatible", "pending"):
        raise ValueError(f"非法兼容决定: {decision}")
    if get(conn, "releases", release_id) is None:
        raise ValueError(f"发布不存在: {release_id}")
    if get(conn, "products", product_id) is None:
        raise ValueError(f"产品不存在: {product_id}")

    previous = query_one(
        conn,
        """SELECT * FROM compatibility c
           WHERE c.release_id = ? AND c.product_id = ?
             AND c.id NOT IN (
                 SELECT supersedes_id FROM compatibility
                 WHERE supersedes_id IS NOT NULL
             )""",
        (release_id, product_id),
    )
    cid = f"cmp_{new_id('x')}"
    insert(conn, "compatibility", {
        "id": cid, "release_id": release_id, "product_id": product_id,
        "decision": decision, "decided_by": actor_id, "decided_at": now_iso(),
        "note": note, "supersedes_id": previous["id"] if previous else None,
        "created_at": now_iso(),
    })
    if previous is not None:
        # 旧决定不再是当前行；重算时只取 supersedes_id IS NULL 的记录
        add_pending(
            conn, "recompute_review",
            f"兼容关系被纠正：发布 {release_id} 对产品 {product_id} "
            f"由 {previous['decision']} 改为 {decision}，相关升级计划须重算",
            ref_type="release", ref_id=release_id,
            detail={"old": previous["decision"], "new": decision},
        )
    return cid


def current_compatibility(conn: sqlite3.Connection, release_id: str,
                          product_id: str) -> sqlite3.Row | None:
    """当前生效的兼容决定 = 未被任何后继决定 supersede 的那一条。"""
    return query_one(
        conn,
        """SELECT * FROM compatibility c
           WHERE c.release_id = ? AND c.product_id = ?
             AND c.id NOT IN (
                 SELECT supersedes_id FROM compatibility
                 WHERE supersedes_id IS NOT NULL
             )""",
        (release_id, product_id),
    )


# ---------------- 停机窗口 ----------------

def add_window(conn: sqlite3.Connection, *, device_id: str, starts_on: str,
               ends_on: str, actor_id: str, note: str | None = None) -> str:
    """登记允许停机窗口（闭区间，按 UTC 日期）。"""
    start = parse_date(starts_on)
    end = parse_date(ends_on)
    if end < start:
        raise ValueError("窗口结束日期不能早于开始日期")
    if get(conn, "devices", device_id) is None:
        raise ValueError(f"设备不存在: {device_id}")
    wid = f"win_{new_id('x')}"
    insert(conn, "maintenance_windows", {
        "id": wid, "device_id": device_id, "starts_on": starts_on,
        "ends_on": ends_on, "note": note, "created_by": actor_id,
        "created_at": now_iso(),
    })
    return wid


def next_open_window(conn: sqlite3.Connection, device_id: str,
                     on_date: date) -> sqlite3.Row | None:
    """返回设备在 on_date 当天或之后的最早窗口（含正在进行的窗口）。

    日期列以 TEXT 存储，比较双方都用 ISO 字符串，避免 SQLite 绑定类型差异。
    """
    on_text = on_date.isoformat()
    return conn.execute(
        """SELECT * FROM maintenance_windows
           WHERE device_id = ? AND ends_on >= ?
           ORDER BY starts_on ASC LIMIT 1""",
        (device_id, on_text),
    ).fetchone()


# ---------------- 生产例外（暂缓） ----------------

def request_exception(conn: sqlite3.Connection, *, device_id: str,
                      starts_on: str, ends_on: str, reason: str,
                      actor_id: str, note: str | None = None) -> str:
    """生产部门提交暂缓申请，初始 pending，等待产线工程师批准。"""
    start = parse_date(starts_on)
    end = parse_date(ends_on)
    if end < start:
        raise ValueError("例外结束日期不能早于开始日期")
    if get(conn, "devices", device_id) is None:
        raise ValueError(f"设备不存在: {device_id}")
    eid = f"exc_{new_id('x')}"
    insert(conn, "production_exceptions", {
        "id": eid, "device_id": device_id, "starts_on": starts_on,
        "ends_on": ends_on, "reason": reason, "state": "pending",
        "decided_by": None, "decided_at": None, "created_at": now_iso(),
        "revoked_at": None, "note": note,
    })
    return eid


def decide_exception(conn: sqlite3.Connection, exception_id: str,
                     approve: bool, actor_id: str,
                     note: str | None = None) -> None:
    """工程师批准/驳回暂缓申请。暂缓只对指定设备和期限有效。"""
    row = get(conn, "production_exceptions", exception_id)
    if row is None:
        raise ValueError(f"生产例外不存在: {exception_id}")
    if row["state"] != "pending":
        raise ValueError(f"例外当前状态 {row['state']} 不可审批")
    conn.execute(
        "UPDATE production_exceptions SET state = ?, decided_by = ?, decided_at = ?, note = COALESCE(?, note) WHERE id = ?",
        ("approved" if approve else "rejected", actor_id, now_iso(),
         note, exception_id),
    )
    if approve:
        add_pending(
            conn, "recompute_review",
            f"生产例外 {exception_id} 已批准（{row['starts_on']}~{row['ends_on']}），"
            "相关升级建议须结合暂缓重算",
            ref_type="device", ref_id=row["device_id"],
        )


def revoke_exception(conn: sqlite3.Connection, exception_id: str,
                     actor_id: str) -> None:
    """提前撤销例外（例如生产窗口提前结束）。"""
    row = get(conn, "production_exceptions", exception_id)
    if row is None:
        raise ValueError(f"生产例外不存在: {exception_id}")
    conn.execute(
        "UPDATE production_exceptions SET state = 'revoked', revoked_at = ? WHERE id = ?",
        (now_iso(), exception_id),
    )
    add_pending(
        conn, "recompute_review",
        f"生产例外 {exception_id} 被撤销，受阻塞的升级计划须重算",
        ref_type="device", ref_id=row["device_id"],
    )


def active_exception(conn: sqlite3.Connection, device_id: str,
                     on_date: date) -> sqlite3.Row | None:
    """返回当天仍有效的已批准例外；到期记录先惰性标记 expired。

    “仍有效”= 已批准且日期闭区间覆盖当天。过期不删除，保留审批历史。
    """
    conn.execute(
        "UPDATE production_exceptions SET state = 'expired' WHERE state = 'approved' AND ends_on < ?",
        (on_date.isoformat(),),
    )
    return conn.execute(
        """SELECT * FROM production_exceptions
           WHERE device_id = ? AND state = 'approved'
             AND starts_on <= ? AND ends_on >= ?
           ORDER BY ends_on DESC LIMIT 1""",
        (device_id, on_date.isoformat(), on_date.isoformat()),
    ).fetchone()
