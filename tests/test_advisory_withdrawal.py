"""公告撤回对建议状态的影响。"""
from __future__ import annotations

import json
from datetime import timedelta

from tests.support import FIXTURES, BaseCase, TODAY
from tests.test_engine import EngineSeedMixin
from upgrade_tracker.approvals import add_window, set_compatibility
from upgrade_tracker.db import get
from upgrade_tracker.engine import (
    ACTIONABLE,
    NEEDS_REVIEW,
    R_ADVISORY_WITHDRAWN,
    recompute_device,
)


class AdvisoryWithdrawalTests(EngineSeedMixin, BaseCase):

    def test_证据公告撤回后可执行建议降级为需复核(self):
        self.seed_verified_world()
        set_compatibility(self.conn, release_id="rel_src-cat_R4",
                          product_id=self.product_of("GW-8"),
                          decision="compatible", actor_id=self.engineer)
        add_window(self.conn, device_id="D3",
                   starts_on=(TODAY + timedelta(days=2)).isoformat(),
                   ends_on=(TODAY + timedelta(days=10)).isoformat(),
                   actor_id=self.engineer)
        self.conn.commit()
        recompute_device(self.conn, "D3")
        reco = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco["status"], ACTIONABLE)

        # 厂商撤回证据公告（保留正文版本历史）
        self.conn.execute(
            "UPDATE announcements SET withdrawn = 1, withdrawn_at = ? WHERE id = ?",
            ("2026-11-25T00:00:00+00:00", "adv_src-adv_ADV-2026-002"),
        )
        self.conn.commit()
        recompute_device(self.conn, "D3")
        reco2 = self.current_reco("D3", "rel_src-cat_R4")
        self.assertEqual(reco2["status"], NEEDS_REVIEW)
        self.assertIn(R_ADVISORY_WITHDRAWN, json.loads(reco2["reasons_json"]))
        # 撤回标记不删除公告与正文，证据仍可追溯
        self.assertTrue(get(self.conn, "announcements",
                            "adv_src-adv_ADV-2026-002")["withdrawn"])
