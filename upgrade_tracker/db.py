"""SQLite 连接、建库与通用数据访问辅助。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def new_id(prefix: str) -> str:
    """生成带前缀的主键，便于在 CLI 输出中辨认实体类型。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数据库连接，强制外键与行工厂。

    内存库 (``:memory:``) 主要供测试使用。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """按 schema.sql 建表并写入版本元数据（幂等）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


# ---------- 小型通用辅助 ----------

def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> str:
    """插入一行，data 必须包含主键 id。"""
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(data.values()),
    )
    return data["id"]


def get(conn: sqlite3.Connection, table: str, row_id: str) -> sqlite3.Row | None:
    """按主键取一行。"""
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def query_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads_json(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def add_pending(
    conn: sqlite3.Connection,
    kind: str,
    summary: str,
    *,
    source_id: str | None = None,
    fetch_run_id: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """向待处理队列追加一条人工待办。"""
    pid = new_id("pend")
    insert(
        conn,
        "pending_queue",
        {
            "id": pid,
            "kind": kind,
            "source_id": source_id,
            "fetch_run_id": fetch_run_id,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "summary": summary,
            "detail_json": dumps_json(detail or {}),
            "status": "open",
            "created_at": _now(),
            "resolved_at": None,
            "resolved_by": None,
            "resolution": None,
        },
    )
    return pid


def _now() -> str:
    # 延迟导入避免循环依赖
    from .timeutil import now_iso

    return now_iso()
