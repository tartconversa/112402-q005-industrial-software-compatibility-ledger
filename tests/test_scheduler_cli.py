"""调度周期与 CLI 端到端集成测试。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from tests.support import FIXTURES, BaseCase, TODAY
from tests.test_engine import EngineSeedMixin
from upgrade_tracker.actors import ensure_actor
from upgrade_tracker.approvals import add_window, set_compatibility
from upgrade_tracker.cli import main as cli_main
from upgrade_tracker.db import query
from upgrade_tracker.engine import recompute_device
from upgrade_tracker.scheduler import run_once


class SchedulerTests(BaseCase):

    def test_run_once_汇总抓取失败与待处理数量(self):
        """单源故障不影响整周期，失败计数与待办一致。"""
        good = "https://vendor.test/catalog.json"
        bad = "https://vendor.test/broken.json"
        self.source("src-cat", "catalog", good, "json_catalog_v1")
        self.source("src-bad", "catalog", bad, "json_catalog_v1")
        self.transport.set_body(good, json.loads((FIXTURES / "catalog.json").read_text()))
        from upgrade_tracker.transport import FetchResult
        self.transport.routes[bad] = FetchResult(
            ok=False, status=503, body=b"", content_type=None,
            etag=None, last_modified=None, error_message="HTTP 503",
        )
        report = run_once(self.conn, self.transport)
        self.assertEqual(report.fetched, 2)
        self.assertEqual(report.failures, 1)
        self.assertGreaterEqual(report.pending_open, 1)


class CliIntegrationTests(EngineSeedMixin, BaseCase):
    """通过 argparse 入口跑完整角色协作流程。"""

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["--db", ":memory:", *argv])
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_完整业务流程_采集确认排期执行闭环(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "tracker.db")
            cat = Path(tmp) / "catalog.json"
            adv = Path(tmp) / "advisory.json"
            inv = Path(tmp) / "devices.json"
            cat.write_text((FIXTURES / "catalog.json").read_text(), encoding="utf-8")
            adv.write_text((FIXTURES / "advisory.json").read_text(), encoding="utf-8")
            inv.write_text((FIXTURES / "inventory.json").read_text(), encoding="utf-8")

            def run(*argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = cli_main(["--db", db_path, *argv])
                self.assertEqual(code, 0, buf.getvalue())
                return buf.getvalue()

            run("init-db")
            run("actor-add", "admin1", "资产管理员甲", "asset_admin")
            run("actor-add", "eng1", "产线工程师甲", "line_engineer")
            run("actor-add", "eng2", "升级工程师乙", "engineer")
            run("actor-add", "lead1", "班组长甲", "team_lead")

            run("source-add", "src-cat", "厂商目录", "catalog",
                cat.resolve().as_uri(), "json_catalog_v1")
            run("source-add", "src-adv", "公告订阅", "advisory",
                adv.resolve().as_uri(), "json_advisory_v1")
            run("source-add", "src-inv", "授权清单", "inventory",
                inv.resolve().as_uri(), "json_inventory_v1")
            run("run-once")

            # 资产管理员确认全部身份
            identities = query(self.conn, "SELECT 1")  # 占位：CLI 库独立，下面用列表输出
            listing = run("identity-list")
            # 直接从 CLI 库读取确认
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            idns = conn.execute("SELECT * FROM build_identities WHERE status = 'raw'").fetchall()
            for idn in idns:
                build = conn.execute(
                    """SELECT b.* FROM builds b JOIN products p ON p.id = b.product_id
                       WHERE p.name = ? AND b.build_number = ? LIMIT 1""",
                    (idn["product_hint"], idn["build_raw"]),
                ).fetchone()
                if build:
                    run("identity-confirm", idn["id"], "admin1",
                        build["product_id"], build["id"])
            conn.close()

            # 工程师登记兼容与窗口
            run("compat-set", "rel_src-cat_R4", self._product_id(db_path, "GW-8"),
                "compatible", "eng1", "--note", "总线复测通过")
            run("window-add", "D3",
                (TODAY + timedelta(days=2)).isoformat(),
                (TODAY + timedelta(days=3)).isoformat(), "eng1")
            run("recompute")

            reco_out = run("reco-list", "--status", "actionable", "--line", "L1")
            self.assertIn("rel_src-cat_R4", reco_out)
            plan_out = run("plan-list", "--line", "L1")
            self.assertIn("scheduled", plan_out)
            plan = json.loads(plan_out)
            plan_id = next(
                p["plan_id"] for p in plan
                if p["release"] == "rel_src-cat_R4" and p["device"] == "D3"
            )
            self.assertEqual(
                next(p["state"] for p in plan if p["plan_id"] == plan_id),
                "scheduled",
            )

            evidence = run("plan-evidence", plan_id)
            self.assertIn("body_sha256", evidence)
            self.assertIn("ADV-2026-002", evidence)

            run("plan-start", plan_id, "lead1")
            run("receipt-submit", plan_id, "lead1", "2.8.2+5")
            import sqlite3
            receipt_id = sqlite3.connect(db_path).execute(
                "SELECT id FROM execution_receipts WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]
            run("receipt-confirm", receipt_id, "eng2", "--approve")
            final_plan = json.loads(run("plan-list", "--line", "L1"))
            self.assertEqual(
                next(p["state"] for p in final_plan if p["plan_id"] == plan_id), "done"
            )
            shown = run("receipt-show", receipt_id)
            self.assertIn("evidence_snapshot", shown)

    @staticmethod
    def _product_id(db_path: str, product_name: str) -> str:
        import sqlite3
        return sqlite3.connect(db_path).execute(
            "SELECT id FROM products WHERE name = ?", (product_name,)
        ).fetchone()[0]

    def test_权限不足的操作被拒绝(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "tracker.db")
            def run(*argv):
                return cli_main(["--db", db_path, *argv])
            run("init-db")
            run("actor-add", "lead1", "班组长甲", "team_lead")
            # 班组长不能确认资产身份
            code = run("identity-confirm", "idn_x", "lead1", "p", "b")
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
