"""公告正文版本化、附件校验与离线补录的可追溯性。"""
from __future__ import annotations

import json

from tests.support import FIXTURES, BaseCase
from upgrade_tracker.db import get, query
from upgrade_tracker.hashing import sha256_text
from upgrade_tracker.ingest import ingest_advisories
from upgrade_tracker.models import AdvisoryPayload
from upgrade_tracker.parsers import parse


class AdvisoryTraceTests(BaseCase):

    def _register_advisory_source(self):
        url = "https://vendor.test/advisories.json"
        self.source("src-adv", "advisory", url, "json_advisory_v1")
        payload = parse(
            "json_advisory_v1",
            json.dumps(json.loads((FIXTURES / "advisory.json").read_text())).encode(),
        )
        return url, payload

    def test_正文变化追加修订且哈希不同(self):
        """同一公告编号正文更新后保留两个 revision，旧正文不被覆盖。"""
        url, payload = self._register_advisory_source()
        self.transport.set_body(url, json.loads((FIXTURES / "advisory.json").read_text()))
        self.fetch("src-adv")
        adv_id = "adv_src-adv_ADV-2026-002"
        rev1 = get(self.conn, "announcements", adv_id)["current_revision_id"]

        doc = json.loads((FIXTURES / "advisory.json").read_text())
        doc["advisories"][0]["body_text"] = "GW-8 2.8.2+5 轮换默认 TLS 证书，并修复内存泄漏。"
        self.transport.set_body(url, doc)
        self.fetch("src-adv")

        revs = query(self.conn,
                     "SELECT * FROM announcement_revisions WHERE announcement_id = ? ORDER BY revision_no",
                     (adv_id,))
        self.assertEqual([r["revision_no"] for r in revs], [1, 2])
        self.assertEqual(revs[0]["body_sha256"],
                         sha256_text("GW-8 2.8.2+5 轮换默认 TLS 证书。"))
        self.assertNotEqual(revs[0]["body_sha256"], revs[1]["body_sha256"])
        self.assertEqual(get(self.conn, "announcements", adv_id)["current_revision_id"],
                         revs[1]["id"])

    def test_正文未变重复抓取不制造新修订(self):
        """内容哈希相同应复用 revision，避免无意义版本噪音。"""
        url, _ = self._register_advisory_source()
        self.transport.set_body(url, json.loads((FIXTURES / "advisory.json").read_text()))
        self.fetch("src-adv")
        self.fetch("src-adv")
        self.assertEqual(
            query(self.conn, "SELECT COUNT(*) AS n FROM announcement_revisions")[0]["n"], 1
        )

    def test_附件校验信息随公告保存(self):
        url = "https://vendor.test/catalog.json"
        self.source("src-cat", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, json.loads((FIXTURES / "catalog.json").read_text()))
        self.fetch("src-cat")
        art = query(self.conn,
                    "SELECT * FROM announcement_artifacts WHERE file_name = 'ADV-2026-001.txt'")[0]
        self.assertEqual(art["checksum_algorithm"], "sha256")
        self.assertEqual(art["checksum_value"], "abc123")

    def test_工程师补录离线公告并对本地附件算哈希(self):
        """补录公告带 manual_entry 标记、操作人，并生成复核待办。"""
        from upgrade_tracker.manual import record_manual_advisory
        import tempfile
        from pathlib import Path

        actor = self.actor("eng1", "engineer")
        with tempfile.TemporaryDirectory() as tmp:
            attach = Path(tmp) / "offline-note.txt"
            attach.write_bytes(b"offline advisory evidence")
            aid = record_manual_advisory(
                self.conn, actor_id=actor, external_key="ADV-OFFLINE-1",
                title="线下会议宣布的补丁", body_text="GW-8 必须升级到 2.8.2+5",
                targets=[("Soft-B", "GW-8", ["2.8.2+5"])],
                attachment_paths=[str(attach)],
            )
        adv = get(self.conn, "announcements", aid)
        self.assertEqual(adv["manual_entry"], 1)
        self.assertIsNone(adv["source_id"])
        art = query(self.conn,
                    "SELECT * FROM announcement_artifacts WHERE revision_id = ?",
                    (adv["current_revision_id"],))[0]
        from upgrade_tracker.hashing import digest
        self.assertEqual(art["observed_sha256"], digest(b"offline advisory evidence"))
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'manual_review'")
        self.assertEqual(len(pending), 1)

    def test_补录公告缺少适用范围时拒绝(self):
        from upgrade_tracker.manual import record_manual_advisory
        actor = self.actor("eng1", "engineer")
        with self.assertRaises(ValueError):
            record_manual_advisory(
                self.conn, actor_id=actor, external_key="X", title="t",
                body_text="b", targets=[],
            )
