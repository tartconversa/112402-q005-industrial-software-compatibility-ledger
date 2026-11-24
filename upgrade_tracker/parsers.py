"""来源解析器：把原始字节翻译成 :mod:`upgrade_tracker.models` 结构。

约定：
- 解析器只做结构校验与翻译，不做版本等价推断；
- 结构不符合约定抛 :class:`ParseError`，由采集服务记入待处理队列；
- 结构合法但列表为空抛 :class:`EmptySuspectError`——
  “空”是可疑情况，绝不允许被解释成“没有新版本”。
"""
from __future__ import annotations

import html
import json
import re
from typing import Any, Callable

from .models import (
    AdvisoryPayload,
    AdvisoryRecord,
    AdvisoryTarget,
    Artifact,
    CatalogPayload,
    DeviceRecord,
    EmptySuspectError,
    InventoryPayload,
    ParseError,
    ReleaseRecord,
)

_VALID_RELEASE_STATES = {"active", "withdrawn", "yanked"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ParseError(message)


def _parse_artifacts(raw: Any, context: str) -> tuple[Artifact, ...]:
    if raw is None:
        return ()
    _require(isinstance(raw, list), f"{context}.artifacts 必须是数组")
    artifacts: list[Artifact] = []
    for index, item in enumerate(raw):
        _require(isinstance(item, dict), f"{context}.artifacts[{index}] 必须是对象")
        for key in ("file_name", "download_url"):
            _require(isinstance(item.get(key), str) and item[key].strip(),
                     f"{context}.artifacts[{index}].{key} 缺失或非字符串")
        artifacts.append(
            Artifact(
                file_name=item["file_name"],
                download_url=item["download_url"],
                size_bytes=item.get("size_bytes"),
                checksum_algorithm=item.get("checksum_algorithm"),
                checksum_value=item.get("checksum_value"),
            )
        )
    return tuple(artifacts)


def _parse_targets(raw: Any, context: str) -> tuple[AdvisoryTarget, ...]:
    _require(isinstance(raw, list) and raw, f"{context}.targets 必须是非空数组")
    targets: list[AdvisoryTarget] = []
    for index, item in enumerate(raw):
        prefix = f"{context}.targets[{index}]"
        _require(isinstance(item, dict), f"{prefix} 必须是对象")
        for key in ("vendor", "product"):
            _require(isinstance(item.get(key), str) and item[key].strip(),
                     f"{prefix}.{key} 缺失或非字符串")
        builds = item.get("raw_builds", [])
        _require(isinstance(builds, list) and all(isinstance(b, str) for b in builds),
                 f"{prefix}.raw_builds 必须是字符串数组")
        targets.append(
            AdvisoryTarget(
                vendor=item["vendor"], product=item["product"],
                raw_builds=tuple(builds),
            )
        )
    return tuple(targets)


def _parse_advisory(item: dict[str, Any], index: int) -> AdvisoryRecord:
    prefix = f"advisories[{index}]"
    for key in ("external_key", "title", "body_text"):
        _require(isinstance(item.get(key), str) and item[key].strip(),
                 f"{prefix}.{key} 缺失或非字符串")
    return AdvisoryRecord(
        external_key=item["external_key"],
        title=item["title"],
        severity=item.get("severity"),
        published_at=item.get("published_at"),
        body_text=item["body_text"],
        content_url=item.get("content_url"),
        withdrawn=bool(item.get("withdrawn", False)),
        targets=_parse_targets(item.get("targets", []), prefix),
        artifacts=_parse_artifacts(item.get("artifacts"), prefix),
    )


def parse_json_catalog_v1(body: bytes) -> CatalogPayload:
    """解析 catalog/v1 JSON：发布目录，可带公告。"""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"catalog JSON 无法解析: {exc}") from exc
    _require(isinstance(doc, dict), "catalog 顶层必须是对象")
    _require(doc.get("schema_version") == "catalog/v1",
             "schema_version 必须为 catalog/v1（页面格式可能已变化）")
    raw_releases = doc.get("releases")
    _require(isinstance(raw_releases, list), "releases 必须是数组")
    if not raw_releases:
        raise EmptySuspectError("版本目录返回空发布列表，需人工核实来源是否异常")

    releases: list[ReleaseRecord] = []
    for index, item in enumerate(raw_releases):
        prefix = f"releases[{index}]"
        _require(isinstance(item, dict), f"{prefix} 必须是对象")
        for key in ("release_id", "vendor", "product", "version", "build_number", "channel"):
            _require(isinstance(item.get(key), str) and item[key].strip(),
                     f"{prefix}.{key} 缺失或非字符串")
        state = str(item.get("state", "active")).lower()
        _require(state in _VALID_RELEASE_STATES, f"{prefix}.state 取值非法: {state}")
        releases.append(
            ReleaseRecord(
                external_id=item["release_id"],
                vendor=item["vendor"],
                product=item["product"],
                version=item["version"],
                build_number=item["build_number"],
                channel=item["channel"],
                posted_at=item.get("posted_at"),
                state=state,
                artifacts=_parse_artifacts(item.get("artifacts"), prefix),
            )
        )

    advisories: list[AdvisoryRecord] = []
    if "advisories" in doc:
        raw_adv = doc["advisories"]
        _require(isinstance(raw_adv, list), "advisories 必须是数组")
        advisories = [_parse_advisory(item, i) for i, item in enumerate(raw_adv)]
    return CatalogPayload(releases=tuple(releases), advisories=tuple(advisories))


def parse_json_advisory_v1(body: bytes) -> AdvisoryPayload:
    """解析 advisory/v1 JSON：公告订阅或邮件导出。"""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"advisory JSON 无法解析: {exc}") from exc
    _require(isinstance(doc, dict), "advisory 顶层必须是对象")
    _require(doc.get("schema_version") == "advisory/v1",
             "schema_version 必须为 advisory/v1（页面格式可能已变化）")
    raw = doc.get("advisories")
    _require(isinstance(raw, list), "advisories 必须是数组")
    if not raw:
        raise EmptySuspectError("公告来源返回空列表，需人工核实来源是否异常")
    return AdvisoryPayload(
        advisories=tuple(_parse_advisory(item, i) for i, item in enumerate(raw))
    )


def parse_json_inventory_v1(body: bytes) -> InventoryPayload:
    """解析 inventory/v1 JSON：授权设备清单。"""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"inventory JSON 无法解析: {exc}") from exc
    _require(isinstance(doc, dict), "inventory 顶层必须是对象")
    _require(doc.get("schema_version") == "inventory/v1",
             "schema_version 必须为 inventory/v1（页面格式可能已变化）")
    raw = doc.get("devices")
    _require(isinstance(raw, list), "devices 必须是数组")
    if not raw:
        raise EmptySuspectError("设备清单返回空列表，需人工核实来源是否异常")
    devices: list[DeviceRecord] = []
    for index, item in enumerate(raw):
        prefix = f"devices[{index}]"
        _require(isinstance(item, dict), f"{prefix} 必须是对象")
        for key in ("device", "name", "line", "product", "build"):
            _require(isinstance(item.get(key), str) and item[key].strip(),
                     f"{prefix}.{key} 缺失或非字符串")
        devices.append(
            DeviceRecord(
                device=item["device"], name=item["name"], line=item["line"],
                product=item["product"], build_raw=item["build"],
            )
        )
    return InventoryPayload(devices=tuple(devices))


# --- 极简 HTML 公告页解析：仅识别约定的 data-* 微格式 ---------------------
# 真实门户接入时按厂商各写一个解析器；页面结构变化会因找不到约定标记而失败，
# 从而进入待处理队列，而不是静默产出空结果。

_H2_RE = re.compile(r"<h2[^>]*data-advisory-key=\"([^\"]+)\"[^>]*>(.*?)</h2>",
                    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _field(fragment: str, name: str) -> str | None:
    match = re.search(
        rf"<[^>]+data-{name}=\"([^\"]+)\"[^>]*>", fragment, re.IGNORECASE
    )
    return html.unescape(match.group(1)).strip() if match else None


def parse_html_advisory_v1(body: bytes) -> AdvisoryPayload:
    """解析带 data-advisory-key 微格式的 HTML 公告页。"""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"HTML 编码无法识别: {exc}") from exc
    chunks = re.split(r"(?=<h2[^>]*data-advisory-key=)", text, flags=re.IGNORECASE)
    advisories: list[AdvisoryRecord] = []
    for chunk in chunks:
        head = _H2_RE.search(chunk)
        if not head:
            continue
        key = html.unescape(head.group(1)).strip()
        title = _strip_tags(head.group(2))
        severity = _field(chunk, "severity")
        published = _field(chunk, "published")
        body_match = re.search(
            r"<div[^>]*data-body=\"1\"[^>]*>(.*?)</div>", chunk,
            re.IGNORECASE | re.DOTALL,
        )
        _require(body_match is not None, f"公告 {key} 缺少 data-body 正文容器")
        body_text = _strip_tags(body_match.group(1))
        _require(bool(body_text), f"公告 {key} 正文为空")
        target_matches = re.findall(
            r"<tr[^>]*data-target=\"1\"[^>]*>(.*?)</tr>",
            chunk, re.IGNORECASE | re.DOTALL,
        )
        _require(bool(target_matches), f"公告 {key} 缺少 data-target 适用范围行")
        targets: list[AdvisoryTarget] = []
        for row in target_matches:
            vendor = _field(row, "vendor")
            product = _field(row, "product")
            builds_raw = _field(row, "builds")
            _require(vendor and product and builds_raw is not None,
                     f"公告 {key} 适用范围行字段不完整")
            targets.append(
                AdvisoryTarget(
                    vendor=vendor, product=product,
                    raw_builds=tuple(b for b in builds_raw.split("|") if b),
                )
            )
        advisories.append(
            AdvisoryRecord(
                external_key=key, title=title, severity=severity,
                published_at=published, body_text=body_text,
                content_url=None,
                withdrawn=_field(chunk, "withdrawn") == "1",
                targets=tuple(targets),
            )
        )
    if not advisories:
        raise EmptySuspectError("HTML 公告页未识别到任何公告标记，页面结构可能已变化")
    return AdvisoryPayload(advisories=tuple(advisories))


PARSERS: dict[str, Callable[[bytes], Any]] = {
    "json_catalog_v1": parse_json_catalog_v1,
    "json_advisory_v1": parse_json_advisory_v1,
    "json_inventory_v1": parse_json_inventory_v1,
    "html_advisory_v1": parse_html_advisory_v1,
}


def parse(parser_name: str, body: bytes) -> Any:
    """按登记的解析器名分派。"""
    try:
        parser = PARSERS[parser_name]
    except KeyError as exc:
        raise ParseError(f"未知解析器: {parser_name}") from exc
    return parser(body)
