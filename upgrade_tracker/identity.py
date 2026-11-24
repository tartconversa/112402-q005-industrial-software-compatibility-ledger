"""身份域：厂商/产品/构建实体，以及原文构建到实体的人工确认。

核心纪律：版本号相近或文件名相同都不构成等价。每条“原文构建”只有经
软件资产管理员显式确认后，才绑定到产品-构建实体；未确认身份的数据
在升级建议中一律降级为 needs_review。
"""
from __future__ import annotations

import re
import sqlite3

from .db import get, insert, new_id, query_one
from .timeutil import now_iso

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """名称转稳定标识（仅小写字母数字，其余压缩为连字符）。"""
    text = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return text or "unknown"


def ensure_vendor(conn: sqlite3.Connection, name: str) -> str:
    """按名称找厂商，不存在则创建，返回厂商 id。"""
    row = query_one(conn, "SELECT id FROM vendors WHERE name = ?", (name,))
    if row:
        return row["id"]
    vid = f"ven_{slug(name)}"
    insert(conn, "vendors", {
        "id": vid, "name": name, "created_at": now_iso(),
    })
    return vid


def ensure_product(conn: sqlite3.Connection, vendor_id: str, name: str) -> str:
    """按厂商+产品名找产品，不存在则创建（新产品身份待资产管理员确认）。"""
    row = query_one(
        conn,
        "SELECT id FROM products WHERE vendor_id = ? AND name = ?",
        (vendor_id, name),
    )
    if row:
        return row["id"]
    pid = f"prod_{slug(vendor_id)}_{slug(name)}"
    insert(conn, "products", {
        "id": pid, "vendor_id": vendor_id, "name": name,
        "created_at": now_iso(),
    })
    return pid


def ensure_build(conn: sqlite3.Connection, product_id: str,
                 version: str, build_number: str) -> str:
    """按 (产品, 版本号, 构建号) 找构建；版本号相同构建号不同即不同构建。"""
    row = query_one(
        conn,
        "SELECT id FROM builds WHERE product_id = ? AND version = ? AND build_number = ?",
        (product_id, version, build_number),
    )
    if row:
        return row["id"]
    bid = f"bld_{new_id('x')}"
    display = f"{version} (build {build_number})"
    insert(conn, "builds", {
        "id": bid, "product_id": product_id, "version": version,
        "build_number": build_number, "display_name": display,
        "created_at": now_iso(),
    })
    return bid


def ensure_identity_row(conn: sqlite3.Connection, vendor_hint: str,
                        product_hint: str, build_raw: str) -> sqlite3.Row:
    """登记一条原文构建身份记录（状态 raw），已存在则直接返回。

    设备清单没有厂商字段（vendor_hint 为空串），因此先按
    (产品提示名, 原文构建) 跨厂商查同一原文构建，避免同物重复登记。
    """
    row = query_one(
        conn,
        "SELECT * FROM build_identities WHERE vendor_hint = ? AND product_hint = ? AND build_raw = ?",
        (vendor_hint, product_hint, build_raw),
    )
    if row:
        return row
    sibling = query_one(
        conn,
        "SELECT * FROM build_identities WHERE product_hint = ? AND build_raw = ? ORDER BY id LIMIT 1",
        (product_hint, build_raw),
    )
    if sibling:
        return sibling
    identity_id = f"idn_{new_id('x')}"
    insert(conn, "build_identities", {
        "id": identity_id,
        "product_hint": product_hint,
        "vendor_hint": vendor_hint,
        "build_raw": build_raw,
        "product_id": None,
        "build_id": None,
        "status": "raw",
        "decided_by": None,
        "decided_at": None,
        "note": None,
        "created_at": now_iso(),
    })
    return get(conn, "build_identities", identity_id)  # type: ignore[return-value]


def confirm_identity(conn: sqlite3.Connection, identity_id: str,
                     actor_id: str, product_id: str, build_id: str,
                     note: str | None = None) -> None:
    """资产管理员确认原文构建与产品-构建实体的身份关系。

    确认后同步回填引用该原文构建的设备与发布，使后续建议可进入自动判定；
    已经确认过的记录允许更正（会重新回填），但驳回与确认的每次决定都
    覆盖 decided_*，历史通过审计字段与设备事件保留。
    """
    identity = get(conn, "build_identities", identity_id)
    if identity is None:
        raise ValueError(f"身份记录不存在: {identity_id}")
    build = get(conn, "builds", build_id)
    if build is None or build["product_id"] != product_id:
        raise ValueError("构建与产品不匹配，不能确认身份")

    conn.execute(
        """UPDATE build_identities
           SET status = 'confirmed', product_id = ?, build_id = ?,
               decided_by = ?, decided_at = ?, note = ?
           WHERE id = ?""",
        (product_id, build_id, actor_id, now_iso(), note, identity_id),
    )

    # 回填设备清单中相同的原文构建（含清单已预挂的临时构建）
    conn.execute(
        """UPDATE devices
           SET product_id = ?, current_build_id = ?
           WHERE product_hint = ? AND current_build_raw = ?""",
        (product_id, build_id, identity["product_hint"], identity["build_raw"]),
    )
    # 回填发布条目（同一产品提示名）
    conn.execute(
        """UPDATE releases
           SET build_id = ?
           WHERE build_id IS NULL AND product_hint = ?""",
        (build_id, identity["product_hint"]),
    )


def reject_identity(conn: sqlite3.Connection, identity_id: str,
                    actor_id: str, note: str) -> None:
    """资产管理员驳回一条身份（例如疑似同名不同物的构建）。"""
    identity = get(conn, "build_identities", identity_id)
    if identity is None:
        raise ValueError(f"身份记录不存在: {identity_id}")
    conn.execute(
        """UPDATE build_identities
           SET status = 'rejected', decided_by = ?, decided_at = ?, note = ?
           WHERE id = ?""",
        (actor_id, now_iso(), note, identity_id),
    )


def resolve_identity(conn: sqlite3.Connection, vendor_hint: str,
                     product_hint: str, build_raw: str) -> sqlite3.Row | None:
    """返回已确认的身份记录；未确认/被驳回返回 None。"""
    return query_one(
        conn,
        """SELECT * FROM build_identities
           WHERE vendor_hint = ? AND product_hint = ? AND build_raw = ?
             AND status = 'confirmed'""",
        (vendor_hint, product_hint, build_raw),
    )
