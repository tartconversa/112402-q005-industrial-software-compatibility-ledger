"""时间与标识工具。

系统内部一律使用 UTC，日期与时间戳以 ISO-8601 文本落库，避免各厂商
门户时区混用导致窗口判断错误。
"""
from __future__ import annotations

from datetime import date, datetime, timezone


def utcnow() -> datetime:
    """返回当前 UTC 时间（可被测试通过替换本函数打桩）。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 文本。"""
    return utcnow().isoformat(timespec="seconds")


def parse_date(value: str) -> date:
    """解析 YYYY-MM-DD 日期；非法输入抛 ValueError。"""
    return date.fromisoformat(value.strip())


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601 时间戳，缺省时区按 UTC 处理。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def today() -> date:
    """当前 UTC 日期（业务窗口按日粒度判断）。"""
    return utcnow().date()


def to_iso(value: datetime) -> str:
    """datetime 转 UTC ISO-8601 文本。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
