from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class GateResult:
    passed: bool
    missing: tuple[str, ...]


_REQUIRED_FIELDS = (
    ("title", "title"),
    ("original_url", "original_url"),
    ("published_at", "published_at"),
    ("publisher", "publisher"),
)

# 只接受可点击追溯的链接协议：file://、javascript:、data: 等一律视为证据不足。
_ALLOWED_URL_SCHEMES = ("http", "https")


def is_web_url(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        return False
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme in _ALLOWED_URL_SCHEMES
            and bool(parsed.hostname)
            and parsed.username is None
        )
    except ValueError:
        return False


def evidence_passes(
    title: Any,
    original_url: Any,
    published_at: Any,
    publisher: Any,
) -> GateResult:
    """证据门槛：四要素齐全且原文链接为 http/https，否则条目降级为 secondary_pending。"""
    values = {
        "title": title,
        "original_url": original_url,
        "published_at": published_at,
        "publisher": publisher,
    }
    missing = tuple(
        name
        for name, _ in _REQUIRED_FIELDS
        if not isinstance(values[name], str)
        or not values[name].strip()
    )
    if "original_url" not in missing and not is_web_url(original_url):
        # 有链接但协议不可追溯：仍计入 original_url 缺失（条目保留，仅降级）。
        missing = (*missing, "original_url")
    return GateResult(passed=not missing, missing=missing)
