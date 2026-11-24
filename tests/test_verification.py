"""下载附件校验：哈希匹配、不匹配入待办。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.support import FIXTURES, BaseCase
from upgrade_tracker.db import get, query
from upgrade_tracker.hashing import digest
from upgrade_tracker.verification import verify_local_artifact


class ArtifactVerificationTests(BaseCase):

    def _seed_artifact(self):
        url = "https://vendor.test/catalog.json"
        self.source("src-cat", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, json.loads((FIXTURES / "catalog.json").read_text()))
        self.fetch("src-cat")
        return query(
            self.conn,
            "SELECT * FROM release_artifacts WHERE file_name = 'gw8-2.8.2-5.bin'",
        )[0]

    def test_哈希匹配时登记观测值且不产生待办(self):
        actor = self.actor("eng1", "engineer")
        artifact = self._seed_artifact()
        payload = b"real firmware bytes"
        # 用实际哈希作为厂商声明值构造匹配场景
        self.conn.execute(
            "UPDATE release_artifacts SET checksum_value = ? WHERE id = ?",
            (digest(payload), artifact["id"]),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gw8.bin"
            path.write_bytes(payload)
            result = verify_local_artifact(
                self.conn, artifact_id=artifact["id"],
                local_path=str(path), actor_id=actor,
            )
        self.assertTrue(result["matched"])
        self.assertEqual(get(self.conn, "release_artifacts", artifact["id"])["observed_sha256"],
                         digest(payload))
        self.assertEqual(
            query(self.conn, "SELECT COUNT(*) AS n FROM pending_queue")[0]["n"], 0
        )

    def test_哈希不符时进入待处理队列(self):
        actor = self.actor("eng1", "engineer")
        artifact = self._seed_artifact()  # 厂商声明 f00d
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gw8.bin"
            path.write_bytes(b"tampered firmware")
            result = verify_local_artifact(
                self.conn, artifact_id=artifact["id"],
                local_path=str(path), actor_id=actor,
            )
        self.assertFalse(result["matched"])
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'manual_review'")
        self.assertEqual(len(pending), 1)
        self.assertIn("校验失败", pending[0]["summary"])
