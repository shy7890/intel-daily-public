import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urljoin, urlunparse
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem

DEFAULT_LIST_URL = (
    "http://www.nea.gov.cn/xwzx/ds_8839d76f7cb542ca8cbaab7122cc9b83.json"
)
PUBLISHER = "国家能源局"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 15
_PUBLISH_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class SourceFetchError(RuntimeError):
    pass


def _strip_markup(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _extract_href(markup: str) -> str | None:
    match = _HREF_RE.search(markup)
    return match.group(1) if match else None


def _parse_publish_time(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), _PUBLISH_TIME_FORMAT).replace(
        tzinfo=TIMEZONE
    )


def normalize_publish_url(publish_url: str, base_url: str) -> str:
    return urljoin(base_url, publish_url)


def parse_nea_payload(
    payload: dict, base_url: str, fetched_at: datetime
) -> list[SourceItem]:
    items: list[SourceItem] = []
    for row in payload.get("datasource", []):
        raw_time = str(row.get("publishTime") or "")
        if not raw_time.strip() or row.get("contentId") is None:
            continue
        published_at = _parse_publish_time(raw_time)
        markup_title = str(row.get("title") or "")
        href = _extract_href(markup_title)
        target_url = href or normalize_publish_url(
            str(row.get("publishUrl") or ""), base_url
        )
        title = _strip_markup(markup_title)
        if not title and not href:
            continue
        keywords_raw = row.get("keywords") or []
        if isinstance(keywords_raw, str):
            keywords = tuple(
                word.strip() for word in keywords_raw.split(",") if word.strip()
            )
        else:
            keywords = tuple(str(word).strip() for word in keywords_raw if word)
        items.append(
            SourceItem(
                external_id=str(row["contentId"]),
                title=title,
                publisher=PUBLISHER,
                published_at=published_at,
                original_url=target_url,
                summary=str(row.get("summary") or ""),
                keywords=keywords,
                fetched_at=fetched_at,
            )
        )
    return items


class NeaAdapter:
    def __init__(
        self,
        list_url: str = DEFAULT_LIST_URL,
        base_url: str = DEFAULT_LIST_URL,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.list_url = list_url
        self.base_url = base_url
        self.user_agent = user_agent

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        request = urllib.request.Request(
            self.list_url, headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raise SourceFetchError(
                f"国家能源局接口返回 {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"国家能源局接口不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"国家能源局接口返回 {status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceFetchError(f"国家能源局接口返回非法 JSON: {exc}") from exc

        items = parse_nea_payload(payload, base_url=self.base_url, fetched_at=now)
        seen_cursor = _max_publish_string(items)
        new_items = _filter_after(items, cursor)
        return FetchBatch(
            items=new_items,
            new_cursor=seen_cursor if new_items else (cursor or ""),
        )


def _max_publish_string(items: list[SourceItem]) -> str:
    if not items:
        return ""
    return max(_publish_string(item.published_at) for item in items)


def _filter_after(
    items: list[SourceItem], cursor: str | None
) -> list[SourceItem]:
    if cursor is None:
        return items
    marker = cursor.strip()
    if not marker:
        return items
    return [
        item
        for item in items
        if _publish_string(item.published_at) > marker
    ]


def _publish_string(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).strftime(_PUBLISH_TIME_FORMAT)
