"""设备清单增量同步与构建变迁审计。"""
from __future__ import annotations

import json

from tests.support import BaseCase, FIXTURES
from upgrade_tracker.db import query


class InventoryTests(BaseCase):

    def _register(self):
        url = "https://vendor.test/devices.json"
        self.source("src-inv", "inventory", url, "json_inventory_v1")
        self.transport.set_body(url, json.loads((FIXTURES / "inventory.json").read_text()))
        return url

    def test_首次同步登记设备并产生初始事件(self):
        self._register()
        self.fetch("src-inv")
        devices = query(self.conn, "SELECT id, line_id, current_build_raw FROM devices ORDER BY id")
        self.assertEqual([d["id"] for d in devices], ["D1", "D2", "D3"])
        events = query(self.conn, "SELECT * FROM inventory_events ORDER BY device_id")
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["old_build_raw"] is None for e in events))

    def test_构建变化追加审计事件且不丢历史(self):
        url = self._register()
        self.fetch("src-inv")
        doc = json.loads((FIXTURES / "inventory.json").read_text())
        doc["devices"][2]["build"] = "2.8.2+5"  # D3 网关清单更新
        self.transport.set_body(url, doc)
        self.fetch("src-inv")
        d3_events = query(self.conn,
                          "SELECT * FROM inventory_events WHERE device_id = 'D3' ORDER BY occurred_at")
        self.assertEqual(len(d3_events), 2)
        self.assertEqual(d3_events[1]["old_build_raw"], "2.7.0")
        self.assertEqual(d3_events[1]["new_build_raw"], "2.8.2+5")

    def test_设备从清单消失不删除而是转人工核实(self):
        url = self._register()
        self.fetch("src-inv")
        doc = json.loads((FIXTURES / "inventory.json").read_text())
        doc["devices"] = [d for d in doc["devices"] if d["device"] != "D2"]
        self.transport.set_body(url, doc)
        self.fetch("src-inv")
        # D2 仍在库
        self.assertEqual(len(query(self.conn, "SELECT * FROM devices WHERE id = 'D2'")), 1)
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'manual_review'")
        self.assertTrue(any("D2" in p["summary"] for p in pending))
