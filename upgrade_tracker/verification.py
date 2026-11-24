"""下载物校验：把实际拿到的附件字节与厂商声明的校验值核对并登记。

附件是不可信数据：本模块只按 URL 读取字节、计算哈希并与厂商声明值比较，
不执行附件内容。调用方应把附件保存在独立目录后再交给本模块。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import add_pending, get, insert, new_id, query_one
from .hashing import digest
from .timeutil import now_iso


def verify_local_artifact(conn: sqlite3.Connection, *, artifact_id: str,
                          local_path: str, actor_id: str) -> dict:
    """对已下载到本地隔离目录的附件计算哈希并核对厂商校验值。

    返回比对结果；哈希不符或缺少厂商校验值时产生待处理项，绝不静默通过。
    """
    artifact = get(conn, "release_artifacts", artifact_id)
    if artifact is None:
        raise ValueError(f"发布附件不存在: {artifact_id}")
    data = Path(local_path).read_bytes()
    observed = digest(data)
    declared = artifact["checksum_value"]
    algorithm = (artifact["checksum_algorithm"] or "sha256").lower()
    if algorithm != "sha256":
        # 表结构允许其他算法，重算对应算法的观测值
        observed = digest(data, algorithm)

    matched = declared is not None and observed.lower() == declared.lower()
    conn.execute(
        "UPDATE release_artifacts SET observed_sha256 = ?, size_bytes = ? WHERE id = ?",
        (digest(data), len(data), artifact_id),
    )
    result = {
        "artifact_id": artifact_id,
        "file_name": artifact["file_name"],
        "observed_sha256": digest(data),
        "declared": declared,
        "matched": matched,
    }
    if declared is None:
        add_pending(
            conn, "manual_review",
            f"附件 {artifact['file_name']} 无厂商校验值，仅有实际观测哈希，需人工确认来源",
            ref_type="artifact", ref_id=artifact_id, detail=result,
        )
    elif not matched:
        add_pending(
            conn, "manual_review",
            f"附件 {artifact['file_name']} 校验失败：厂商声明 {declared}，实际 {observed}",
            ref_type="artifact", ref_id=artifact_id, detail=result,
        )
    conn.commit()
    return result
