"""采集链路：增量快照、条件请求、失败入队、空结果保护。"""
from __future__ import annotations

import json

from tests.support import FIXTURES, BaseCase
from upgrade_tracker.db import query
from upgrade_tracker.models import EmptySuspectError, ParseError
from upgrade_tracker.parsers import parse
from upgrade_tracker.transport import FetchResult


class FetchTests(BaseCase):

    def _register_catalog(self):
        url = "https://vendor.test/catalog.json"
        self.source("src-cat", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, json.loads((FIXTURES / "catalog.json").read_text()))
        return url

    def test_首次抓取保存原始快照与哈希并落库发布(self):
        """原始字节、sha256、发布与附件必须全部可追溯。"""
        url = self._register_catalog()
        summary = self.fetch("src-cat")
        self.assertEqual(summary.outcome, "ok")

        snap = query(self.conn, "SELECT * FROM raw_snapshots WHERE source_id = 'src-cat'")[0]
        import hashlib
        self.assertEqual(snap["sha256"], hashlib.sha256(snap["body"]).hexdigest())
        run = query(self.conn, "SELECT * FROM fetch_runs WHERE source_id = 'src-cat'")[0]
        self.assertEqual(run["outcome"], "ok")
        self.assertEqual(run["raw_snapshot_id"], snap["id"])

        releases = query(self.conn, "SELECT id, channel, state FROM releases ORDER BY id")
        self.assertEqual([r["id"] for r in releases],
                         ["rel_src-cat_R1", "rel_src-cat_R2",
                          "rel_src-cat_R3", "rel_src-cat_R4"])
        # 同版本号不同渠道是两个独立发布，附件文件名与哈希各自保留
        arts = query(self.conn,
                     "SELECT file_name, checksum_value FROM release_artifacts ORDER BY file_name")
        names = {a["file_name"] for a in arts}
        self.assertIn("plcx-4.2.0-117-stable.bin", names)
        self.assertIn("plcx-4.2.0-117-regional.bin", names)

    def test_条件请求_304_视为未变化且不产生新快照(self):
        """ETag 命中时不重复落库，也不产生待办。"""
        url = self._register_catalog()
        self.fetch("src-cat")
        self.transport.routes[url] = FetchResult(
            ok=True, status=304, body=b"", content_type=None,
            etag=None, last_modified=None, not_modified=True,
        )
        summary = self.fetch("src-cat")
        self.assertEqual(summary.outcome, "not_modified")
        self.assertEqual(
            query(self.conn,
                  "SELECT COUNT(*) AS n FROM raw_snapshots WHERE source_id = 'src-cat'")[0]["n"],
            1,
        )
        self.assertEqual(
            query(self.conn, "SELECT COUNT(*) AS n FROM pending_queue")[0]["n"], 0
        )

    def test_网络失败进入待处理队列而不是当作无新版本(self):
        """抓取失败必须产生 fetch_failure 待办，禁止静默。"""
        url = "https://vendor.test/broken.json"
        self.source("src-broken", "catalog", url, "json_catalog_v1")
        self.transport.routes[url] = FetchResult(
            ok=False, status=0, body=b"", content_type=None,
            etag=None, last_modified=None, error_message="网络错误: 连接超时",
        )
        summary = self.fetch("src-broken")
        self.assertEqual(summary.outcome, "network_error")
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'fetch_failure'")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "open")

    def test_页面格式变化产生_parse_failure_待办且保留快照(self):
        """schema_version 不匹配是格式变化，快照仍要保留用于排查。"""
        url = "https://vendor.test/changed.json"
        self.source("src-changed", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, {"schema_version": "catalog/v2", "releases": []})
        summary = self.fetch("src-changed")
        self.assertEqual(summary.outcome, "parse_error")
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'parse_failure'")
        self.assertEqual(len(pending), 1)
        self.assertIsNotNone(pending[0]["detail_json"])
        self.assertEqual(
            query(self.conn, "SELECT COUNT(*) AS n FROM raw_snapshots")[0]["n"], 1
        )

    def test_空列表触发_empty_suspect_而非解释为没有新版本(self):
        """解析成功但内容为空必须进待处理队列。"""
        url = "https://vendor.test/empty.json"
        self.source("src-empty", "catalog", url, "json_catalog_v1")
        self.transport.set_body(url, {"schema_version": "catalog/v1", "releases": []})
        summary = self.fetch("src-empty")
        self.assertEqual(summary.outcome, "empty_suspect")
        self.assertEqual(
            query(self.conn,
                  "SELECT COUNT(*) AS n FROM pending_queue WHERE kind = 'empty_suspect'")[0]["n"],
            1,
        )
        # 没有任何发布被写入
        self.assertEqual(query(self.conn, "SELECT COUNT(*) AS n FROM releases")[0]["n"], 0)

    def test_目录中消失的发布标记撤回并要求人工核实(self):
        """增量变化：条目消失等同厂商撤回信号，进入待办。"""
        url = self._register_catalog()
        self.fetch("src-cat")
        payload = json.loads((FIXTURES / "catalog.json").read_text())
        payload["releases"] = [r for r in payload["releases"] if r["release_id"] != "R4"]
        self.transport.set_body(url, payload)
        self.fetch("src-cat")
        r4 = query(self.conn, "SELECT * FROM releases WHERE id = 'rel_src-cat_R4'")[0]
        self.assertEqual(r4["state"], "withdrawn")
        pending = query(self.conn, "SELECT * FROM pending_queue WHERE kind = 'manual_review'")
        self.assertTrue(any("rel_src-cat_R4" in p["summary"] for p in pending))


class ParserTests(BaseCase):

    def test_解析器对非法结构直接报错(self):
        with self.assertRaises(ParseError):
            parse("json_catalog_v1", b'{"schema_version": "catalog/v1"}')
        with self.assertRaises(ParseError):
            parse("json_inventory_v1", b"not-json")

    def test_空结果专用异常与普通解析错误区分(self):
        with self.assertRaises(EmptySuspectError):
            parse("json_advisory_v1",
                  json.dumps({"schema_version": "advisory/v1", "advisories": []}).encode())

    def test_html_微格式解析与结构变化检测(self):
        from upgrade_tracker.parsers import parse_html_advisory_v1
        html = (
            '<html><body>'
            '<h2 data-advisory-key="ADV-H1">网关补丁</h2>'
            '<span data-severity="high"></span>'
            '<span data-published="2026-11-20"></span>'
            '<div data-body="1">修复越权访问</div>'
            '<table><tr data-target="1">'
            '<td data-vendor="Soft-B"></td><td data-product="GW-8"></td>'
            '<td data-builds="2.8.2+5"></td></tr></table>'
            '</body></html>'
        )
        payload = parse_html_advisory_v1(html.encode())
        self.assertEqual(payload.advisories[0].external_key, "ADV-H1")
        self.assertEqual(payload.advisories[0].targets[0].raw_builds, ("2.8.2+5",))
        with self.assertRaises(EmptySuspectError):
            parse_html_advisory_v1("<html>改版后的页面没有约定标记</html>".encode())
