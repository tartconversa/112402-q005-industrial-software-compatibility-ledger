"""命令行入口：覆盖采集、补录、身份确认、兼容/例外审批、建议重算与回执全流程。

用法示例：
  upgrade-tracker init-db
  upgrade-tracker source add ...
  upgrade-tracker run-once
  upgrade-tracker identity confirm ...
  upgrade-tracker recompute
  upgrade-tracker plan list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db as dbmod
from .actors import ROLES, ensure_actor, require_role
from .approvals import (
    add_window,
    decide_exception,
    request_exception,
    revoke_exception,
    set_compatibility,
)
from .db import get, init_db, loads_json, query
from .engine import recompute_all, recompute_device
from .execution import confirm_receipt, start_plan, submit_receipt
from .fetch import fetch_one, register_source
from .manual import record_manual_advisory
from .scheduler import list_pending, resolve_pending, run_once
from .identity import confirm_identity, reject_identity
from .verification import verify_local_artifact

DEFAULT_DB = "upgrade_tracker.db"


# ---------------- 输出辅助 ----------------

def _print_rows(rows, columns: list[str]) -> None:
    if not rows:
        print("(无记录)")
        return
    widths = {c: max(len(c), *(len(str(r[c] if c in r.keys() else "")) for r in rows))
              for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    for row in rows:
        print("  ".join(str(row[c] if c in row.keys() else "").ljust(widths[c])
                        for c in columns))


def _json_row(row) -> dict:
    return dict(row) if row is not None else {}


def _connect(args) -> "dbmod.sqlite3.Connection":
    conn = dbmod.connect(args.db)
    init_db(conn)
    return conn


# ---------------- 子命令实现 ----------------

def cmd_init_db(args) -> None:
    conn = _connect(args)
    print(f"数据库已初始化：{args.db}")
    conn.close()


def cmd_actor_add(args) -> None:
    conn = _connect(args)
    ensure_actor(conn, args.actor_id, args.name, args.role)
    print(f"操作人已登记：{args.actor_id}（{args.role}）")
    conn.close()


def cmd_source_add(args) -> None:
    conn = _connect(args)
    register_source(
        conn, source_id=args.source_id, display_name=args.name,
        kind=args.kind, url=args.url, parser=args.parser,
        credentials=args.credentials,
    )
    conn.commit()
    print(f"来源已登记：{args.source_id}")
    conn.close()


def cmd_source_fetch(args) -> None:
    conn = _connect(args)
    summary = fetch_one(conn, args.source_id)
    print(f"抓取结果：{summary.outcome} — {summary.detail}")
    if summary.pending_id:
        print(f"已生成待处理项：{summary.pending_id}")
    conn.close()


def cmd_run_once(args) -> None:
    conn = _connect(args)
    report = run_once(conn)
    print(f"周期完成：来源 {report.fetched} 个，失败 {report.failures} 个，"
          f"待处理 {report.pending_open} 条，重算设备 {report.devices_recomputed} 台")
    conn.close()


def cmd_pending_list(args) -> None:
    conn = _connect(args)
    rows = list_pending(conn, kind=args.kind, status=args.status)
    _print_rows(rows, ["id", "kind", "status", "summary", "created_at"])
    conn.close()


def cmd_pending_resolve(args) -> None:
    conn = _connect(args)
    resolve_pending(conn, args.pending_id, args.actor, args.resolution)
    print(f"待办 {args.pending_id} 已关闭")
    conn.close()


def cmd_identity_list(args) -> None:
    conn = _connect(args)
    rows = query(conn, "SELECT * FROM build_identities ORDER BY status, id")
    _print_rows(rows, ["id", "vendor_hint", "product_hint", "build_raw",
                       "status", "product_id", "build_id", "decided_by"])
    conn.close()


def cmd_identity_confirm(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("asset_admin",))
    confirm_identity(conn, args.identity_id, args.actor,
                     args.product_id, args.build_id, args.note)
    conn.commit()
    print(f"身份已确认：{args.identity_id} -> 产品 {args.product_id} / 构建 {args.build_id}")
    conn.close()


def cmd_identity_reject(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("asset_admin",))
    reject_identity(conn, args.identity_id, args.actor, args.note)
    conn.commit()
    print(f"身份已驳回：{args.identity_id}")
    conn.close()


def cmd_compat_set(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("line_engineer",))
    cid = set_compatibility(
        conn, release_id=args.release_id, product_id=args.product_id,
        decision=args.decision, actor_id=args.actor, note=args.note,
    )
    conn.commit()
    print(f"兼容决定已登记：{cid}（{args.decision}）")
    conn.close()


def cmd_window_add(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("line_engineer", "engineer"))
    wid = add_window(conn, device_id=args.device, starts_on=args.starts,
                     ends_on=args.ends, actor_id=args.actor, note=args.note)
    conn.commit()
    print(f"停机窗口已登记：{wid}")
    conn.close()


def cmd_exception_request(args) -> None:
    conn = _connect(args)
    eid = request_exception(
        conn, device_id=args.device, starts_on=args.starts, ends_on=args.ends,
        reason=args.reason, actor_id=args.actor, note=args.note,
    )
    conn.commit()
    print(f"暂缓申请已提交：{eid}（待工程师批准）")
    conn.close()


def cmd_exception_decide(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("line_engineer", "engineer"))
    decide_exception(conn, args.exception_id, args.approve, args.actor, args.note)
    conn.commit()
    print("例外已" + ("批准" if args.approve else "驳回"))
    conn.close()


def cmd_exception_revoke(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("line_engineer", "engineer"))
    revoke_exception(conn, args.exception_id, args.actor)
    conn.commit()
    print(f"例外已撤销：{args.exception_id}")
    conn.close()


def cmd_advisory_manual(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("engineer", "line_engineer"))
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    aid = record_manual_advisory(
        conn, actor_id=args.actor,
        external_key=payload["external_key"], title=payload["title"],
        body_text=payload["body_text"],
        targets=[(t["vendor"], t["product"], t["raw_builds"])
                 for t in payload["targets"]],
        severity=payload.get("severity"),
        published_at=payload.get("published_at"),
        attachment_paths=payload.get("attachment_paths"),
    )
    print(f"离线公告已补录：{aid}（含待复核待办）")
    conn.close()


def cmd_artifact_verify(args) -> None:
    """对下载到隔离目录的附件计算哈希并核对厂商校验值。"""
    conn = _connect(args)
    require_role(conn, args.actor, ("engineer", "line_engineer", "asset_admin"))
    result = verify_local_artifact(
        conn, artifact_id=args.artifact_id,
        local_path=args.file, actor_id=args.actor,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["matched"]:
        print("警告：校验未通过或缺少厂商校验值，已生成待处理项", file=sys.stderr)
    conn.close()


def cmd_recompute(args) -> None:
    conn = _connect(args)
    if args.device:
        recompute_device(conn, args.device)
        print(f"设备 {args.device} 的建议已重算")
    else:
        n = recompute_all(conn)
        print(f"全部 {n} 台设备的建议已重算")
    conn.close()


def cmd_reco_list(args) -> None:
    conn = _connect(args)
    sql = """SELECT r.*, d.line_id, d.id AS device, rel.id AS release
             FROM recommendations r
             JOIN devices d ON d.id = r.device_id
             JOIN releases rel ON rel.id = r.release_id
             WHERE r.id NOT IN (
                 SELECT supersedes_id FROM recommendations
                 WHERE supersedes_id IS NOT NULL
             )"""
    params: tuple = ()
    if args.status:
        sql += " AND r.status = ?"
        params = (args.status,)
    if args.line:
        sql += " AND d.line_id = ?"
        params = params + (args.line,)
    sql += " ORDER BY d.line_id, d.id, r.status"
    rows = query(conn, sql, params)
    out = []
    for row in rows:
        data = dict(row)
        data["reasons"] = loads_json(row["reasons_json"], [])
        data["announcements"] = loads_json(row["linked_announcement_ids"], [])
        out.append(data)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    conn.close()


def cmd_plan_list(args) -> None:
    conn = _connect(args)
    sql = """SELECT up.*, d.line_id, d.id AS device, w.starts_on, w.ends_on,
                    r.status AS recommendation_status, r.reasons_json
             FROM upgrade_plans up
             JOIN devices d ON d.id = up.device_id
             JOIN recommendations r ON r.id = up.recommendation_id
             LEFT JOIN maintenance_windows w ON w.id = up.window_id
             WHERE 1=1"""
    params: list = []
    if args.line:
        sql += " AND d.line_id = ?"
        params.append(args.line)
    if args.state:
        sql += " AND up.state = ?"
        params.append(args.state)
    sql += " ORDER BY d.line_id, d.id"
    rows = query(conn, sql, tuple(params))
    display = []
    for row in rows:
        display.append({
            "plan_id": row["id"], "line": row["line_id"], "device": row["device"],
            "release": row["release_id"], "state": row["state"],
            "window": f"{row['starts_on']}~{row['ends_on']}" if row["starts_on"] else None,
            "blocked_reason": row["blocked_reason"],
            "recommendation": row["recommendation_status"],
            "reasons": loads_json(row["reasons_json"], []),
        })
    print(json.dumps(display, ensure_ascii=False, indent=2))
    conn.close()


def cmd_plan_evidence(args) -> None:
    """班组长查看证据链：公告正文版本、附件哈希、兼容与例外审批。"""
    conn = _connect(args)
    plan = get(conn, "upgrade_plans", args.plan_id)
    if plan is None:
        raise SystemExit(f"计划不存在：{args.plan_id}")
    rec = get(conn, "recommendations", plan["recommendation_id"])
    adv_ids = loads_json(rec["linked_announcement_ids"], [])
    evidence = []
    for adv_id in adv_ids:
        adv = get(conn, "announcements", adv_id)
        rev = get(conn, "announcement_revisions", adv["current_revision_id"])
        artifacts = query(
            conn, "SELECT * FROM announcement_artifacts WHERE revision_id = ?",
            (rev["id"],),
        )
        evidence.append({
            "announcement": adv["external_key"],
            "manual_entry": bool(adv["manual_entry"]),
            "withdrawn": bool(adv["withdrawn"]),
            "revision_no": rev["revision_no"],
            "body_sha256": rev["body_sha256"],
            "artifacts": [dict(a) for a in artifacts],
        })
    release_artifacts = query(
        conn, "SELECT * FROM release_artifacts WHERE release_id = ?",
        (plan["release_id"],),
    )
    print(json.dumps({
        "plan_id": plan["id"],
        "device": plan["device_id"],
        "state": plan["state"],
        "announcement_evidence": evidence,
        "release_artifacts": [dict(a) for a in release_artifacts],
        "reasons": loads_json(rec["reasons_json"], []),
    }, ensure_ascii=False, indent=2))
    conn.close()


def cmd_plan_start(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("team_lead", "engineer"))
    start_plan(conn, args.plan_id, args.actor)
    print(f"计划 {args.plan_id} 已开始执行")
    conn.close()


def cmd_receipt_submit(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("team_lead",))
    rid = submit_receipt(conn, args.plan_id, args.actor,
                         args.actual_build, result=args.result, note=args.note)
    print(f"回执已提交：{rid}，等待工程师确认")
    conn.close()


def cmd_receipt_confirm(args) -> None:
    conn = _connect(args)
    require_role(conn, args.actor, ("engineer",))
    confirm_receipt(conn, args.receipt_id, args.actor, args.approve, args.note)
    print("回执已" + ("确认，升级闭环" if args.approve else "驳回"))
    conn.close()


def cmd_receipt_show(args) -> None:
    conn = _connect(args)
    receipt = get(conn, "execution_receipts", args.receipt_id)
    if receipt is None:
        raise SystemExit(f"回执不存在：{args.receipt_id}")
    data = dict(receipt)
    data["evidence_snapshot"] = loads_json(receipt["evidence_snapshot_json"], {})
    del data["evidence_snapshot_json"]
    print(json.dumps(data, ensure_ascii=False, indent=2))
    conn.close()


def cmd_audit_reco(args) -> None:
    """查看某设备某发布的建议变化历史（为什么结论变了）。"""
    conn = _connect(args)
    rows = query(
        conn,
        """SELECT * FROM recommendation_history
           WHERE device_id = ? AND release_id = ? ORDER BY computed_at""",
        (args.device, args.release),
    )
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="upgrade-tracker",
                                     description="工业设备升级跟踪系统")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    p = sub.add_parser("actor-add")
    p.add_argument("actor_id")
    p.add_argument("name")
    p.add_argument("role", choices=ROLES)
    p.set_defaults(func=cmd_actor_add)

    p = sub.add_parser("source-add")
    p.add_argument("source_id")
    p.add_argument("name")
    p.add_argument("kind", choices=["catalog", "advisory", "inventory"])
    p.add_argument("url")
    p.add_argument("parser",
                   choices=["json_catalog_v1", "json_advisory_v1",
                            "json_inventory_v1", "html_advisory_v1"])
    p.add_argument("--credentials")
    p.set_defaults(func=cmd_source_add)

    p = sub.add_parser("source-fetch")
    p.add_argument("source_id")
    p.set_defaults(func=cmd_source_fetch)

    sub.add_parser("run-once").set_defaults(func=cmd_run_once)

    p = sub.add_parser("pending-list")
    p.add_argument("--kind")
    p.add_argument("--status", default="open")
    p.set_defaults(func=cmd_pending_list)

    p = sub.add_parser("pending-resolve")
    p.add_argument("pending_id")
    p.add_argument("actor")
    p.add_argument("resolution")
    p.set_defaults(func=cmd_pending_resolve)

    sub.add_parser("identity-list").set_defaults(func=cmd_identity_list)

    p = sub.add_parser("identity-confirm")
    p.add_argument("identity_id")
    p.add_argument("actor")
    p.add_argument("product_id")
    p.add_argument("build_id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_identity_confirm)

    p = sub.add_parser("identity-reject")
    p.add_argument("identity_id")
    p.add_argument("actor")
    p.add_argument("note")
    p.set_defaults(func=cmd_identity_reject)

    p = sub.add_parser("compat-set")
    p.add_argument("release_id")
    p.add_argument("product_id")
    p.add_argument("decision", choices=["compatible", "incompatible", "pending"])
    p.add_argument("actor")
    p.add_argument("--note")
    p.set_defaults(func=cmd_compat_set)

    p = sub.add_parser("window-add")
    p.add_argument("device")
    p.add_argument("starts")
    p.add_argument("ends")
    p.add_argument("actor")
    p.add_argument("--note")
    p.set_defaults(func=cmd_window_add)

    p = sub.add_parser("exception-request")
    p.add_argument("device")
    p.add_argument("starts")
    p.add_argument("ends")
    p.add_argument("reason")
    p.add_argument("actor")
    p.add_argument("--note")
    p.set_defaults(func=cmd_exception_request)

    p = sub.add_parser("exception-decide")
    p.add_argument("exception_id")
    p.add_argument("actor")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", dest="approve", action="store_false")
    p.add_argument("--note")
    p.set_defaults(func=cmd_exception_decide)

    p = sub.add_parser("exception-revoke")
    p.add_argument("exception_id")
    p.add_argument("actor")
    p.set_defaults(func=cmd_exception_revoke)

    p = sub.add_parser("advisory-manual")
    p.add_argument("file")
    p.add_argument("actor")
    p.set_defaults(func=cmd_advisory_manual)

    p = sub.add_parser("recompute")
    p.add_argument("--device")
    p.set_defaults(func=cmd_recompute)

    p = sub.add_parser("reco-list")
    p.add_argument("--status", choices=["actionable", "needs_review", "not_applicable"])
    p.add_argument("--line")
    p.set_defaults(func=cmd_reco_list)

    p = sub.add_parser("artifact-verify")
    p.add_argument("artifact_id")
    p.add_argument("file")
    p.add_argument("actor")
    p.set_defaults(func=cmd_artifact_verify)

    p = sub.add_parser("plan-list")
    p.add_argument("--line")
    p.add_argument("--state")
    p.set_defaults(func=cmd_plan_list)

    p = sub.add_parser("plan-evidence")
    p.add_argument("plan_id")
    p.set_defaults(func=cmd_plan_evidence)

    p = sub.add_parser("plan-start")
    p.add_argument("plan_id")
    p.add_argument("actor")
    p.set_defaults(func=cmd_plan_start)

    p = sub.add_parser("receipt-submit")
    p.add_argument("plan_id")
    p.add_argument("actor")
    p.add_argument("actual_build")
    p.add_argument("--result", default="success",
                   choices=["success", "failed", "rolled_back"])
    p.add_argument("--note")
    p.set_defaults(func=cmd_receipt_submit)

    p = sub.add_parser("receipt-confirm")
    p.add_argument("receipt_id")
    p.add_argument("actor")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", dest="approve", action="store_false")
    p.add_argument("--note")
    p.set_defaults(func=cmd_receipt_confirm)

    p = sub.add_parser("receipt-show")
    p.add_argument("receipt_id")
    p.set_defaults(func=cmd_receipt_show)

    p = sub.add_parser("audit-reco")
    p.add_argument("device")
    p.add_argument("release")
    p.set_defaults(func=cmd_audit_reco)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, PermissionError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
