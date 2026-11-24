"""来源解析后的规范化数据结构。

解析器只负责把来源原文翻译为这些结构，不直接写库；
这样“格式变化”可以在落库前被捕获并进入待处理队列。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SourceKind = Literal["catalog", "advisory", "inventory"]


@dataclass(frozen=True)
class Artifact:
    """下载附件及其厂商声明的校验信息。"""

    file_name: str
    download_url: str
    size_bytes: int | None = None
    checksum_algorithm: str | None = None
    checksum_value: str | None = None


@dataclass(frozen=True)
class ReleaseRecord:
    """版本目录中的一条发布。"""

    external_id: str
    vendor: str
    product: str
    version: str
    build_number: str
    channel: str
    posted_at: str | None
    state: str                      # active / withdrawn / yanked（原文小写化）
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdvisoryTarget:
    """公告适用范围条目；raw_builds 保留厂商原文，不做等价推断。"""

    vendor: str
    product: str
    raw_builds: tuple[str, ...]


@dataclass(frozen=True)
class AdvisoryRecord:
    """一条公告及其当前正文版本。"""

    external_key: str
    title: str
    severity: str | None
    published_at: str | None
    body_text: str
    content_url: str | None
    withdrawn: bool
    targets: tuple[AdvisoryTarget, ...]
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeviceRecord:
    """授权设备清单中的一台设备。"""

    device: str
    name: str
    line: str
    product: str
    build_raw: str


@dataclass(frozen=True)
class CatalogPayload:
    """catalog 来源解析结果：发布列表，可选公告列表。"""

    releases: tuple[ReleaseRecord, ...] = ()
    advisories: tuple[AdvisoryRecord, ...] = ()


@dataclass(frozen=True)
class AdvisoryPayload:
    advisories: tuple[AdvisoryRecord, ...]


@dataclass(frozen=True)
class InventoryPayload:
    devices: tuple[DeviceRecord, ...]


class ParseError(Exception):
    """来源格式不符合解析器约定（页面结构变化等）。"""


class EmptySuspectError(ParseError):
    """解析成功但结果为空：不能解释为“没有新版本”。"""
