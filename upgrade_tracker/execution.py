"""升级执行与回执工作流。

班组长查看受影响设备、窗口、阻塞原因与证据来源，作业后提交完成回执；
工程师确认升级结果。回执会固化执行时实际采用的构建与全部批准证据，
之后任何重算都不能改写这台设备的历史记录。
"""
from __future__ import annotations

import sqlite3

from . import db as dbmod
from .db import dumps_json, get, insert, new_id, loads_json, query_one
from .engine import recompute_device
from .timeutil import now_iso


def _evidence_snapshot(conn: sqlite3.Connection, plan: sqlite3.Row) -> str:
    """冻结执行时刻的建议、兼容、例外与公告证据。"""
    rec = get(conn, "recommendations", plan["recommendation_id"])
    release = get(conn, "releases", plan["release_id"])
    build = get(conn, "builds", release["build_id"]) if release["build_id"] else None
    compat = None
    if build is not None:
        row = conn.execute(
            """SELECT c.* FROM compatibility c
               WHERE c.release_id = ? AND c.product_id = ?
                 AND c.id NOT IN (
                     SELECT supersedes_id FROM compatibility
                     WHERE supersedes_id IS NOT NULL
                 )""",
            (release["id"], build["product_id"]),
        ).fetchone()
        compat = dict(row) if row else None
    adv_ids = loads_json(rec["linked_announcement_ids"], []) if rec else []
    advisories = []
    for adv_id in adv_ids:
        adv = get(conn, "announcements", adv_id)
        rev = get(conn, "announcement_revisions", adv["current_revision_id"]) if adv else None
        advisories.append({
            "announcement_id": adv_id,
            "external_key": adv["external_key"] if adv else None,
            "manual_entry": adv["manual_entry"] if adv else None,
            "revision_id": rev["id"] if rev else None,
            "body_sha256": rev["body_sha256"] if rev else None,
            "withdrawn": adv["withdrawn"] if adv else None,
        })
    exceptions = [dict(r) for r in conn.execute(
        "SELECT * FROM production_exceptions WHERE device_id = ? ORDER BY id",
        (plan["device_id"],),
    ).fetchall()]
    window = get(conn, "maintenance_windows", plan["window_id"]) if plan["window_id"] else None
    return dumps_json({
        "recommendation": dict(rec) if rec else None,
        "release": dict(release) if release else None,
        "build": dict(build) if build else None,
        "compatibility": compat,
        "maintenance_window": dict(window) if window else None,
        "production_exceptions": exceptions,
        "announcements": advisories,
        "frozen_at": now_iso(),
    })


def start_plan(conn: sqlite3.Connection, plan_id: str, actor_id: str) -> None:
    """班组长接单，计划进入执行中。"""
    plan = get(conn, "upgrade_plans", plan_id)
    if plan is None:
        raise ValueError(f"升级计划不存在: {plan_id}")
    if plan["state"] != "scheduled":
        raise ValueError(f"计划状态为 {plan['state']}，只有已排期计划可开始执行")
    conn.execute(
        "UPDATE upgrade_plans SET state = 'in_progress', updated_at = ? WHERE id = ?",
        (now_iso(), plan_id),
    )
    dbmod.insert(conn, "upgrade_plan_events", {
        "id": f"pev_{new_id('x')}", "plan_id": plan_id,
        "event_type": "started", "from_state": "scheduled",
        "to_state": "in_progress", "actor_id": actor_id, "note": None,
        "occurred_at": now_iso(),
    })
    conn.commit()


def submit_receipt(conn: sqlite3.Connection, plan_id: str, actor_id: str,
                   actual_build_raw: str, result: str = "success",
                   note: str | None = None) -> str:
    """班组长提交完成回执（工程师确认前不算升级完成）。"""
    if result not in ("success", "failed", "rolled_back"):
        raise ValueError("回执结果必须是 success / failed / rolled_back")
    plan = get(conn, "upgrade_plans", plan_id)
    if plan is None:
        raise ValueError(f"升级计划不存在: {plan_id}")
    if plan["state"] not in ("in_progress", "scheduled"):
        raise ValueError(f"计划状态为 {plan['state']}，不可提交回执")
    existing = query_one(
        conn, "SELECT id FROM execution_receipts WHERE plan_id = ?", (plan_id,)
    )
    if existing is not None:
        raise ValueError("该计划已存在回执，请由工程师确认或驳回后再操作")

    release = get(conn, "releases", plan["release_id"])
    receipt_id = f"rcp_{new_id('x')}"
    insert(conn, "execution_receipts", {
        "id": receipt_id, "plan_id": plan_id, "device_id": plan["device_id"],
        "target_release_id": plan["release_id"],
        "target_build_id": release["build_id"],
        "target_build_raw": actual_build_raw,
        "submitted_by": actor_id, "submitted_at": now_iso(),
        "confirmed_by": None, "confirmed_at": None, "result": result,
        "engineer_note": note,
        "evidence_snapshot_json": _evidence_snapshot(conn, plan),
        "created_at": now_iso(),
    })
    dbmod.insert(conn, "upgrade_plan_events", {
        "id": f"pev_{new_id('x')}", "plan_id": plan_id,
        "event_type": "receipt_submitted", "from_state": plan["state"],
        "to_state": plan["state"], "actor_id": actor_id,
        "note": f"回执 {receipt_id}，结果 {result}", "occurred_at": now_iso(),
    })
    conn.commit()
    return receipt_id


def confirm_receipt(conn: sqlite3.Connection, receipt_id: str, actor_id: str,
                    approved: bool, note: str | None = None) -> None:
    """工程师确认或驳回回执；确认成功才闭环并更新设备当前构建。"""
    receipt = get(conn, "execution_receipts", receipt_id)
    if receipt is None:
        raise ValueError(f"回执不存在: {receipt_id}")
    if receipt["confirmed_at"] is not None:
        raise ValueError("回执已确认，不能重复操作")
    plan = get(conn, "upgrade_plans", receipt["plan_id"])

    if not approved or receipt["result"] != "success":
        conn.execute(
            "UPDATE execution_receipts SET confirmed_by = ?, confirmed_at = ?, engineer_note = ? WHERE id = ?",
            (actor_id, now_iso(), note, receipt_id),
        )
        conn.execute(
            "UPDATE upgrade_plans SET state = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
            ("工程师驳回回执或升级未成功，需重新安排", now_iso(), plan["id"]),
        )
        dbmod.insert(conn, "upgrade_plan_events", {
            "id": f"pev_{new_id('x')}", "plan_id": plan["id"],
            "event_type": "receipt_rejected", "from_state": plan["state"],
            "to_state": "blocked", "actor_id": actor_id, "note": note,
            "occurred_at": now_iso(),
        })
        conn.commit()
        return

    conn.execute(
        "UPDATE execution_receipts SET confirmed_by = ?, confirmed_at = ?, engineer_note = COALESCE(?, engineer_note) WHERE id = ?",
        (actor_id, now_iso(), note, receipt_id),
    )
    # 设备当前构建更新为实际采用的构建，并留 inventory 事件
    conn.execute(
        "UPDATE devices SET current_build_id = COALESCE(?, current_build_id), current_build_raw = ?, updated_at = ? WHERE id = ?",
        (receipt["target_build_id"], receipt["target_build_raw"], now_iso(),
         receipt["device_id"]),
    )
    insert(conn, "inventory_events", {
        "id": f"iev_{new_id('x')}", "device_id": receipt["device_id"],
        "source_id": None, "old_build_raw": None,
        "new_build_raw": receipt["target_build_raw"],
        "raw_snapshot_id": None, "occurred_at": now_iso(),
    })
    conn.execute(
        "UPDATE upgrade_plans SET state = 'done', blocked_reason = NULL, updated_at = ? WHERE id = ?",
        (now_iso(), plan["id"]),
    )
    dbmod.insert(conn, "upgrade_plan_events", {
        "id": f"pev_{new_id('x')}", "plan_id": plan["id"],
        "event_type": "receipt_confirmed", "from_state": plan["state"],
        "to_state": "done", "actor_id": actor_id, "note": note,
        "occurred_at": now_iso(),
    })
    conn.commit()
    # 设备构建已变，重算其余建议；已闭环计划在 _sync_plan 中被冻结保护
    recompute_device(conn, receipt["device_id"])
