"""执行回执工作流与“已执行历史不可变”纪律。"""
from __future__ import annotations

import json
from datetime import timedelta

from tests.support import BaseCase, FIXTURES, TODAY
from tests.test_engine import EngineSeedMixin
from upgrade_tracker.actors import ensure_actor
from upgrade_tracker.approvals import add_window, set_compatibility
from upgrade_tracker.db import get, query
from upgrade_tracker.engine import ACTIONABLE, recompute_device
from upgrade_tracker.execution import confirm_receipt, start_plan, submit_receipt


def _iso(d):
    return d.isoformat()


class ExecutionTests(EngineSeedMixin, BaseCase):

    def _prepare_scheduled_plan(self, device: str = "D3", release: str = "rel_src-cat_R4",
                                product: str = "GW-8"):
        self.seed_verified_world()
        lead = ensure_actor(self.conn, "lead1", "班组长甲", "team_lead")
        set_compatibility(self.conn, release_id=release,
                          product_id=self.product_of(product),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id=device,
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, device)
        plan = self.current_plan(device, release)
        self.assertEqual(plan["state"], "scheduled")
        return plan, lead

    def test_班组长提交回执工程师确认后计划闭环(self):
        plan, lead = self._prepare_scheduled_plan()
        start_plan(self.conn, plan["id"], lead)
        receipt_id = submit_receipt(self.conn, plan["id"], lead, "2.8.2+5")
        receipt = get(self.conn, "execution_receipts", receipt_id)
        self.assertIsNone(receipt["confirmed_at"])
        # 确认前计划仍是 in_progress
        self.assertEqual(get(self.conn, "upgrade_plans", plan["id"])["state"], "in_progress")

        confirm_receipt(self.conn, receipt_id, self.engineer, True)
        done = get(self.conn, "upgrade_plans", plan["id"])
        self.assertEqual(done["state"], "done")
        device = get(self.conn, "devices", "D3")
        self.assertEqual(device["current_build_raw"], "2.8.2+5")

    def test_回执固化当时的公告与批准证据快照(self):
        plan, lead = self._prepare_scheduled_plan()
        start_plan(self.conn, plan["id"], lead)
        receipt_id = submit_receipt(self.conn, plan["id"], lead, "2.8.2+5")
        confirm_receipt(self.conn, receipt_id, self.engineer, True)
        receipt = get(self.conn, "execution_receipts", receipt_id)
        snapshot = json.loads(receipt["evidence_snapshot_json"])
        self.assertIn("announcements", snapshot)
        self.assertTrue(snapshot["announcements"])
        linked = snapshot["announcements"][0]
        revision = get(self.conn, "announcement_revisions", linked["revision_id"])
        self.assertEqual(linked["body_sha256"], revision["body_sha256"])
        self.assertEqual(snapshot["compatibility"]["decision"], "compatible")

    def test_工程师驳回回执后计划重回阻塞(self):
        plan, lead = self._prepare_scheduled_plan()
        start_plan(self.conn, plan["id"], lead)
        receipt_id = submit_receipt(self.conn, plan["id"], lead, "2.8.2+5",
                                    result="failed", note="刷写中断")
        confirm_receipt(self.conn, receipt_id, self.engineer, False, "需复测")
        self.assertEqual(get(self.conn, "upgrade_plans", plan["id"])["state"], "blocked")

    def test_已执行设备的历史不被后续撤回与重算改写(self):
        """版本撤回/兼容纠正只影响未执行计划，done 计划与回执永久保留。"""
        plan, lead = self._prepare_scheduled_plan()
        start_plan(self.conn, plan["id"], lead)
        receipt_id = submit_receipt(self.conn, plan["id"], lead, "2.8.2+5")
        confirm_receipt(self.conn, receipt_id, self.engineer, True)

        # 事后厂商撤回 R4 且兼容结论被纠正
        self.conn.execute(
            "UPDATE releases SET state = 'withdrawn' WHERE id = 'rel_src-cat_R4'"
        )
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="incompatible", actor_id=self.engineer,
                          note="事后发现问题")
        self.conn.commit()
        recompute_device(self.conn, "D3")

        done = get(self.conn, "upgrade_plans", plan["id"])
        self.assertEqual(done["state"], "done")
        receipt = get(self.conn, "execution_receipts", receipt_id)
        self.assertEqual(receipt["target_build_raw"], "2.8.2+5")
        self.assertIsNotNone(receipt["confirmed_at"])
        # 设备保留当时采用的构建
        self.assertEqual(get(self.conn, "devices", "D3")["current_build_raw"], "2.8.2+5")

    def test_非班组长不能提交回执(self):
        from upgrade_tracker.actors import require_role
        plan, _ = self._prepare_scheduled_plan()
        # eng1 是产线工程师，不在班组长角色集合内
        with self.assertRaises(PermissionError):
            require_role(self.conn, "eng1", ("team_lead",))

    def test_重复提交回执被拒绝(self):
        plan, lead = self._prepare_scheduled_plan()
        start_plan(self.conn, plan["id"], lead)
        submit_receipt(self.conn, plan["id"], lead, "2.8.2+5")
        with self.assertRaises(ValueError):
            submit_receipt(self.conn, plan["id"], lead, "2.8.2+5")
