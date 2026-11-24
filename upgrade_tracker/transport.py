"""HTTP 传输抽象。

真实环境使用 :class:`HttpTransport`（urllib，无第三方依赖）；
测试与离线演示使用可注入的 FakeTransport。传输层不解析内容，
也不把“拿到空字节”当成正常业务结果——交由上层判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib import error, request


@dataclass(frozen=True)
class FetchResult:
    """一次抓取的原始结果。"""

    ok: bool
    status: int                      # HTTP 状态码；网络层失败为 0
    body: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    error_message: str | None = None
    not_modified: bool = False


class Transport(Protocol):
    def get(self, url: str, *, etag: str | None = None,
            last_modified: str | None = None,
            credentials: str | None = None) -> FetchResult:
        ...


class HttpTransport:
    """基于 urllib 的最小传输实现，支持 ETag/Last-Modified 条件请求。"""

    def get(self, url: str, *, etag: str | None = None,
            last_modified: str | None = None,
            credentials: str | None = None) -> FetchResult:
        # 本地文件用于离线公告导入与演示；按安全规程这些文件存放在独立目录，
        # 传输层只读取字节，不执行任何内容。
        if url.startswith("file://") or "://" not in url:
            return self._get_local(url, etag, last_modified)
        headers = {"User-Agent": "cnc-upgrade-tracker/0.1", "Accept": "*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        req = request.Request(url, headers=headers)
        try:
            with request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL 来自登记来源
                body = resp.read()
                return FetchResult(
                    ok=True,
                    status=resp.status,
                    body=body,
                    content_type=resp.headers.get("Content-Type"),
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                )
        except error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(True, 304, b"", None, etag, last_modified,
                                   not_modified=True)
            return FetchResult(
                ok=False, status=exc.code, body=b"", content_type=None,
                etag=None, last_modified=None,
                error_message=f"HTTP {exc.code}: {exc.reason}",
            )
        except (error.URLError, TimeoutError, OSError) as exc:
            return FetchResult(
                ok=False, status=0, body=b"", content_type=None,
                etag=None, last_modified=None,
                error_message=f"网络错误: {exc}",
            )

    def _get_local(self, url: str, etag: str | None,
                   last_modified: str | None) -> FetchResult:
        path = Path(url[7:] if url.startswith("file://") else url)
        if not path.is_file():
            return FetchResult(
                ok=False, status=0, body=b"", content_type=None,
                etag=None, last_modified=None,
                error_message=f"本地文件不存在: {path}",
            )
        body = path.read_bytes()
        return FetchResult(
            ok=True, status=200, body=body,
            content_type="application/octet-stream",
            etag=etag, last_modified=last_modified,
        )
