"""角色与操作人登记辅助。"""
from __future__ import annotations

import sqlite3

from .db import get, insert
from .timeutil import now_iso

ROLES = ("asset_admin", "line_engineer", "team_lead", "engineer")


def ensure_actor(conn: sqlite3.Connection, actor_id: str, name: str,
                 role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"角色必须是 {ROLES} 之一")
    if get(conn, "actors", actor_id) is not None:
        return actor_id
    insert(conn, "actors", {
        "id": actor_id, "name": name, "role": role, "created_at": now_iso(),
    })
    conn.commit()
    return actor_id


def require_role(conn: sqlite3.Connection, actor_id: str, roles: tuple[str, ...]) -> None:
    actor = get(conn, "actors", actor_id)
    if actor is None:
        raise ValueError(f"操作人不存在: {actor_id}")
    if actor["role"] not in roles:
        raise PermissionError(
            f"操作人 {actor_id} 角色为 {actor['role']}，需要 {roles} 之一"
        )
