"""跨模块回归：补录公告进入证据链、审计历史不重复。"""
from __future__ import annotations

from tests.support import BaseCase
from tests.test_engine import EngineSeedMixin
from upgrade_tracker.actors import ensure_actor
from upgrade_tracker.db import query
from upgrade_tracker.engine import recompute_device
from upgrade_tracker.manual import record_manual_advisory


class ManualEvidenceTests(EngineSeedMixin, BaseCase):

    def test_补录公告重算后进入建议证据链(self):
        """离线公告与抓取公告同等参与适用范围匹配。"""
        self.seed_verified_world()
        engineer = ensure_actor(self.conn, "eng2", "升级工程师乙", "engineer")
        recompute_device(self.conn, "D3")
        before = query(
            self.conn,
            """SELECT linked_announcement_ids FROM recommendations r
               WHERE device_id='D3' AND release_id='rel_src-cat_R4'
                 AND r.id NOT IN (SELECT supersedes_id FROM recommendations
                                  WHERE supersedes_id IS NOT NULL)""",
        )[0]["linked_announcement_ids"]

        record_manual_advisory(
            self.conn, actor_id=engineer,
            external_key="ADV-OFFLINE-9",
            title="线下渠道紧急补丁",
            body_text="GW-8 2.8.2+5 紧急修复，口头通知补录。",
            targets=[("Soft-B", "GW-8", ["2.8.2+5"])],
            severity="critical",
        )
        self.conn.commit()
        recompute_device(self.conn, "D3")
        after = query(
            self.conn,
            """SELECT linked_announcement_ids FROM recommendations r
               WHERE device_id='D3' AND release_id='rel_src-cat_R4'
                 AND r.id NOT IN (SELECT supersedes_id FROM recommendations
                                  WHERE supersedes_id IS NOT NULL)""",
        )[0]["linked_announcement_ids"]
        import json
        self.assertNotIn("adv_manual_ADV-OFFLINE-9", before)
        self.assertIn("adv_manual_ADV-OFFLINE-9", after)
        self.assertIn("adv_src-adv_ADV-2026-002", json.loads(after))

    def test_相同输入连续重算只保留一条历史(self):
        self.seed_verified_world()
        recompute_device(self.conn, "D3")
        recompute_device(self.conn, "D3")
        recompute_device(self.conn, "D3")
        rows = query(
            self.conn,
            "SELECT COUNT(*) AS n FROM recommendation_history WHERE device_id='D3'"
        )[0]["n"]
        # 每个发布首次计算各一条；后续相同输入不得追加
        release_count = query(self.conn, "SELECT COUNT(*) AS n FROM releases")[0]["n"]
        self.assertEqual(rows, release_count)
