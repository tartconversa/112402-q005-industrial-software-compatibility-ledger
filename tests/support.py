"""测试公用夹具：内存数据库、可控传输层与固定时钟。"""
from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from upgrade_tracker.db import connect, init_db
from upgrade_tracker.fetch import register_source
from upgrade_tracker.transport import FetchResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# 评估逻辑固定在“今天”2026-11-25，窗口与例外围绕该日期构造
TODAY = date(2026, 11, 25)


class FakeTransport:
    """按 URL 返回预置响应的传输层；可模拟 304、HTTP 错误与网络故障。"""

    def __init__(self, routes: dict[str, FetchResult] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, str | None, str | None]] = []

    def set_body(self, url: str, payload: dict | list | str,
                 content_type: str = "application/json") -> None:
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.routes[url] = FetchResult(
            ok=True, status=200, body=body, content_type=content_type,
            etag=None, last_modified=None,
        )

    def get(self, url, *, etag=None, last_modified=None, credentials=None):
        self.calls.append((url, etag, last_modified))
        if url not in self.routes:
            return FetchResult(False, 0, b"", None, None, None,
                               error_message="网络错误: 未登记的测试 URL")
        result = self.routes[url]
        if result.not_modified:
            return FetchResult(True, 304, b"", None, etag, last_modified,
                               not_modified=True)
        return result


class BaseCase(unittest.TestCase):
    """每个用例独立内存库，并把引擎时钟固定到 TODAY。"""

    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.transport = FakeTransport()
        self._patch_clock()

    def _patch_clock(self) -> None:
        import upgrade_tracker.engine as engine_mod
        self._orig_today = engine_mod.today
        engine_mod.today = lambda: TODAY
        import upgrade_tracker.approvals as approvals_mod
        approvals_mod.today = lambda: TODAY

    def tearDown(self) -> None:
        import upgrade_tracker.engine as engine_mod
        engine_mod.today = self._orig_today
        import upgrade_tracker.approvals as approvals_mod
        del approvals_mod.today  # 恢复模块内 from-import 的名字
        self.conn.close()

    # ---------- 业务快捷构造 ----------

    def source(self, source_id: str, kind: str, url: str, parser: str) -> None:
        register_source(
            self.conn, source_id=source_id, display_name=source_id,
            kind=kind, url=url, parser=parser,
        )
        self.conn.commit()

    def actor(self, actor_id: str, role: str, name: str | None = None) -> str:
        from upgrade_tracker.actors import ensure_actor
        return ensure_actor(self.conn, actor_id, name or actor_id, role)

    def fetch(self, source_id: str):
        from upgrade_tracker.fetch import fetch_one
        return fetch_one(self.conn, source_id, self.transport)

    def catalog_url(self, payload: dict, url: str = "https://vendor.test/catalog.json") -> str:
        self.transport.set_body(url, payload)
        return url

    def advisory_url(self, payload: dict, url: str = "https://vendor.test/advisories.json") -> str:
        self.transport.set_body(url, payload)
        return url

    def inventory_url(self, payload: dict, url: str = "https://vendor.test/devices.json") -> str:
        self.transport.set_body(url, payload)
        return url
