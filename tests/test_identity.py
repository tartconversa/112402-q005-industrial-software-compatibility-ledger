"""身份确认纪律：版本号/文件名相近不自动等价，未确认身份一律转复核。"""
from __future__ import annotations

import json

from tests.support import FIXTURES, BaseCase
from upgrade_tracker.db import get, query
from upgrade_tracker.engine import (
    NEEDS_REVIEW,
    R_BUILD_IDENTITY,
    R_DEVICE_IDENTITY,
    R_REJECTED_IDENTITY,
    recompute_device,
)


class IdentityTests(BaseCase):

    def _seed(self):
        url = "https://vendor.test/catalog.json"
        self.source("src-cat", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, json.loads((FIXTURES / "catalog.json").read_text()))
        self.fetch("src-cat")
        inv_url = "https://vendor.test/devices.json"
        self.source("src-inv", "inventory", inv_url, "json_inventory_v1")
        self.transport.set_body(inv_url, json.loads((FIXTURES / "inventory.json").read_text()))
        self.fetch("src-inv")

    def test_采集后身份记录为_raw_且建议需复核(self):
        self._seed()
        rows = query(self.conn, "SELECT * FROM build_identities ORDER BY id")
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == "raw" for r in rows))
        recos = recompute_device(self.conn, "D1")
        # 未确认身份的设备，任何发布都不能自动给可执行结论；
        # 已撤回发布仍按不适用处理（撤回是独立硬事实），其余全部需复核。
        self.assertNotIn("actionable", {r["status"] for r in recos})
        self.assertTrue(
            all(r["status"] == NEEDS_REVIEW
                for r in recos if "release_withdrawn" not in json.loads(r["reasons_json"]))
        )
        reasons = {r["release_id"]: json.loads(r["reasons_json"]) for r in recos}
        self.assertTrue(any(R_DEVICE_IDENTITY in v for v in reasons.values()))

    def test_资产管理员确认后设备与发布身份回填(self):
        self._seed()
        admin = self.actor("admin1", "asset_admin")
        identity = query(self.conn,
                         "SELECT * FROM build_identities WHERE product_hint = 'PLC-X' AND build_raw = '4.2.0+117'")[0]
        build = query(self.conn,
                      "SELECT b.* FROM builds b JOIN products p ON p.id = b.product_id "
                      "WHERE p.name = 'PLC-X' AND b.build_number = '4.2.0+117' LIMIT 1")[0]
        from upgrade_tracker.identity import confirm_identity
        confirm_identity(self.conn, identity["id"], admin, build["product_id"], build["id"])
        self.conn.commit()
        device = get(self.conn, "devices", "D2")
        self.assertEqual(device["current_build_id"], build["id"])
        release = get(self.conn, "releases", "rel_src-cat_R1")
        self.assertEqual(release["build_id"], build["id"])
        # 同版本号的区域站发布绑定到同一个“构建实体”的前提是人工确认；
        # 渠道差异仍保留为两条独立 release，不做自动等价
        release_r2 = get(self.conn, "releases", "rel_src-cat_R2")
        self.assertNotEqual(release["id"], release_r2["id"])

    def test_驳回身份的构建进入驳回原因而非自动匹配(self):
        self._seed()
        admin = self.actor("admin1", "asset_admin")
        identity = query(self.conn,
                         "SELECT * FROM build_identities WHERE product_hint = 'GW-8' AND build_raw = '2.7.0'")[0]
        from upgrade_tracker.identity import reject_identity
        reject_identity(self.conn, identity["id"], admin, "同名构建疑似不同产线分支")
        self.conn.commit()
        recos = recompute_device(self.conn, "D3")
        all_reasons = {x for r in recos for x in json.loads(r["reasons_json"])}
        self.assertIn(R_REJECTED_IDENTITY, all_reasons)
        self.assertNotIn("actionable", {r["status"] for r in recos})
        self.assertTrue(
            all(r["status"] == NEEDS_REVIEW
                for r in recos if "release_withdrawn" not in json.loads(r["reasons_json"]))
        )

    def test_同版本号不同构建号是不同构建实体(self):
        """4.2.0 与 4.2.0+117 这类差异必须落成两条 build 记录。"""
        self._seed()
        builds = query(self.conn,
                       "SELECT b.* FROM builds b JOIN products p ON p.id = b.product_id "
                       "WHERE p.name = 'PLC-X'")
        keys = {(b["version"], b["build_number"]) for b in builds}
        self.assertIn(("4.2.0", "4.2.0+117"), keys)
        # 设备 D1 的 4.1.9+99 与目录构建各自独立
        self.assertTrue(any("4.1.9" in k[1] for k in keys))
