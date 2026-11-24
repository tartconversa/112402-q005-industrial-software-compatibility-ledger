"""建议引擎：三态判定、例外/窗口/撤回/兼容纠正触发的重算与历史保留。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from tests.support import FIXTURES, BaseCase, TODAY
from upgrade_tracker.actors import ensure_actor
from upgrade_tracker.approvals import (
    add_window,
    decide_exception,
    request_exception,
    revoke_exception,
    set_compatibility,
)
from upgrade_tracker.db import get, query
from upgrade_tracker.engine import (
    ACTIONABLE,
    NEEDS_REVIEW,
    NOT_APPLICABLE,
    R_ALREADY_CURRENT,
    R_COMPAT_PENDING,
    R_EXCEPTION_ACTIVE,
    R_INCOMPATIBLE,
    R_NO_WINDOW,
    R_WITHDRAWN,
    recompute_device,
)
from upgrade_tracker.identity import confirm_identity


def _iso(d: date) -> str:
    return d.isoformat()


class EngineSeedMixin:
    """构造一个身份全部确认、可直接评估的基线场景。"""

    def seed_verified_world(self):
        cat_url = "https://vendor.test/catalog.json"
        self.source("src-cat", "catalog", cat_url, "json_catalog_v1")
        self.transport.set_body(cat_url, json.loads((FIXTURES / "catalog.json").read_text()))
        self.fetch("src-cat")
        adv_url = "https://vendor.test/advisories.json"
        self.source("src-adv", "advisory", adv_url, "json_advisory_v1")
        self.transport.set_body(adv_url, json.loads((FIXTURES / "advisory.json").read_text()))
        self.fetch("src-adv")
        inv_url = "https://vendor.test/devices.json"
        self.source("src-inv", "inventory", inv_url, "json_inventory_v1")
        self.transport.set_body(inv_url, json.loads((FIXTURES / "inventory.json").read_text()))
        self.fetch("src-inv")

        admin = ensure_actor(self.conn, "admin1", "资产管理员甲", "asset_admin")
        identities = query(self.conn, "SELECT * FROM build_identities")
        for idn in identities:
            build = query(
                self.conn,
                """SELECT b.* FROM builds b JOIN products p ON p.id = b.product_id
                   JOIN vendors v ON v.id = p.vendor_id
                   WHERE p.name = ? AND b.build_number = ? LIMIT 1""",
                (idn["product_hint"], idn["build_raw"]),
            )
            if build:
                confirm_identity(self.conn, idn["id"], admin,
                                 build[0]["product_id"], build[0]["id"])
        self.conn.commit()
        self.engineer = ensure_actor(self.conn, "eng1", "产线工程师甲", "line_engineer")
        return admin

    def current_reco(self, device_id: str, release_id: str):
        return query(
            self.conn,
            """SELECT * FROM recommendations WHERE device_id = ? AND release_id = ?
               AND id NOT IN (
                   SELECT supersedes_id FROM recommendations
                   WHERE supersedes_id IS NOT NULL
               )""",
            (device_id, release_id),
        )[0]

    def current_plan(self, device_id: str, release_id: str):
        return query(
            self.conn,
            """SELECT * FROM upgrade_plans WHERE device_id = ? AND release_id = ?
               AND state IN ('blocked','scheduled','in_progress','done','canceled')
               ORDER BY updated_at DESC LIMIT 1""",
            (device_id, release_id),
        )[0]

    def product_of(self, product_name: str) -> str:
        return query(self.conn, "SELECT id FROM products WHERE name = ?",
                     (product_name,))[0]["id"]


class RecommendationTests(EngineSeedMixin, BaseCase):

    def test_无兼容审核时结论为需复核(self):
        """身份确认但兼容矩阵未登记，不能直接给可执行。"""
        self.seed_verified_world()
        recompute_device(self.conn, "D3")
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], NEEDS_REVIEW)
        self.assertIn(R_COMPAT_PENDING, json.loads(reco["reasons_json"]))

    def test_兼容确认且有窗口时建议可执行并自动排期(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")

        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], ACTIONABLE)
        self.assertEqual(json.loads(reco["linked_announcement_ids"]),
                         ["adv_src-adv_ADV-2026-002"])
        plan = self.current_plan("D3", "rel_src-cat_R4")
        self.assertEqual(plan["state"], "scheduled")
        self.assertIsNotNone(plan["window_id"])

    def test_可执行但缺少停机窗口时计划阻塞并写明原因(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], ACTIONABLE)
        self.assertIn(R_NO_WINDOW, json.loads(reco["reasons_json"]))
        plan = self.current_plan("D3", "rel_src-cat_R4")
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("停机窗口", plan["blocked_reason"])

    def test_生效中的生产例外阻塞排期且例外到期自动恢复(self):
        """暂缓只对指定设备与期限有效；到期后重算自动回到可排期。"""
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=10)),
                   actor_id=self.engineer)
        eid = request_exception(
            self.conn, device_id="D3",
            starts_on=_iso(TODAY - timedelta(days=1)),
            ends_on=_iso(TODAY + timedelta(days=5)),
            reason="连续生产窗口", actor_id=self.engineer,
        )
        decide_exception(self.conn, eid, True, self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")

        plan = self.current_plan("D3", "rel_src-cat_R4")
        self.assertEqual(plan["state"], "blocked")
        self.assertIn("生产例外", plan["blocked_reason"])
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertTrue(
            any(r.startswith(R_EXCEPTION_ACTIVE) for r in json.loads(reco["reasons_json"]))
        )

        # 模拟例外到期日之后重算：惰性过期 + 自动排期
        recompute_device(self.conn, "D3", on_date=TODAY + timedelta(days=6))
        plan2 = query(
            self.conn,
            """SELECT * FROM upgrade_plans WHERE device_id = 'D3' AND release_id = 'rel_src-cat_R4'
               ORDER BY updated_at DESC LIMIT 1""",
        )[0]
        self.assertEqual(plan2["state"], "scheduled")
        self.assertEqual(get(self.conn, "production_exceptions", eid)["state"], "expired")

    def test_未批准的_pending_例外不产生暂缓效力(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        request_exception(
            self.conn, device_id="D3", starts_on=_iso(TODAY),
            ends_on=_iso(TODAY + timedelta(days=5)),
            reason="待批冻结", actor_id=self.engineer,
        )
        self.conn.commit()
        recompute_device(self.conn, "D3")
        self.assertEqual(self.current_plan("D3", "rel_src-cat_R4")["state"], "scheduled")

    def test_撤回发布判不适用且取消未执行计划(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        self.assertEqual(self.current_plan("D3", "rel_src-cat_R4")["state"], "scheduled")

        # 厂商撤回 R4
        self.conn.execute(
            "UPDATE releases SET state = 'withdrawn', state_changed_at = ? WHERE id = 'rel_src-cat_R4'",
            ("2026-11-24T00:00:00+00:00",),
        )
        self.conn.commit()
        recompute_device(self.conn, "D3")
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], NOT_APPLICABLE)
        self.assertIn(R_WITHDRAWN, json.loads(reco["reasons_json"]))
        plan = self.current_plan("D3", "rel_src-cat_R4")
        self.assertEqual(plan["state"], "canceled")

    def test_兼容关系被纠正为不兼容时计划取消并留待办(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")

        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="incompatible", actor_id=self.engineer,
                          note="现场复测发现总线冲突")
        self.conn.commit()
        recompute_device(self.conn, "D3")
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], NOT_APPLICABLE)
        self.assertIn(R_INCOMPATIBLE, json.loads(reco["reasons_json"]))
        self.assertEqual(self.current_plan("D3", "rel_src-cat_R4")["state"], "canceled")
        pending = query(self.conn,
                        "SELECT * FROM pending_queue WHERE kind = 'recompute_review'")
        self.assertTrue(any("兼容关系被纠正" in p["summary"] for p in pending))
        # 旧兼容结论仍可追溯
        compats = query(self.conn,
                        "SELECT decision, supersedes_id FROM compatibility WHERE release_id = 'rel_src-cat_R4'")
        self.assertEqual(len(compats), 2)

    def test_设备已在目标构建上时判不适用(self):
        """D2 当前构建就是 4.2.0+117，对 R1 不应再建议升级。"""
        self.seed_verified_world()
        recompute_device(self.conn, "D2")
        reco = self.current_reco("D2", "rel_src-cat_R1")
        self.assertEqual(reco["status"], NOT_APPLICABLE)
        self.assertIn(R_ALREADY_CURRENT, json.loads(reco["reasons_json"]))

    def test_输入未变时重算不产生新建议行(self):
        self.seed_verified_world()
        recompute_device(self.conn, "D1")
        before = query(self.conn, "SELECT COUNT(*) AS n FROM recommendations")[0]["n"]
        recompute_device(self.conn, "D1")
        after = query(self.conn, "SELECT COUNT(*) AS n FROM recommendations")[0]["n"]
        self.assertEqual(before, after)

    def test_建议变化保留完整历史链(self):
        """每次结论变化在 recommendation_history 留痕，可解释结论演变。"""
        self.seed_verified_world()
        recompute_device(self.conn, "D3")
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        history = query(
            self.conn,
            "SELECT status FROM recommendation_history WHERE device_id = 'D3' AND release_id = 'rel_src-cat_R4' ORDER BY computed_at",
        )
        self.assertEqual([h["status"] for h in history][0], NEEDS_REVIEW)
        self.assertEqual([h["status"] for h in history][-1], ACTIONABLE)

    def test_撤销例外立即触发重算回到可排期(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=_iso(TODAY + timedelta(days=2)),
                   ends_on=_iso(TODAY + timedelta(days=3)),
                   actor_id=self.engineer)
        eid = request_exception(
            self.conn, device_id="D3", starts_on=_iso(TODAY),
            ends_on=_iso(TODAY + timedelta(days=10)),
            reason="连续生产", actor_id=self.engineer,
        )
        decide_exception(self.conn, eid, True, self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        self.assertEqual(self.current_plan("D3", "rel_src-cat_R4")["state"], "blocked")

        revoke_exception(self.conn, eid, self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        self.assertEqual(self.current_plan("D3", "rel_src-cat_R4")["state"], "scheduled")
