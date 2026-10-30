import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_PREFIX = "https://static.cninfo.com.cn/"
DEFAULT_COLUMNS = ("szse", "bj")
PUBLISHER = "巨潮资讯网"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20
REQUEST_INTERVAL = 1.5
PAGE_SIZE = 30
MAX_PAGES = 3
_FORM_BASE = {
    "tabName": "fulltext",
    "sortName": "",
    "sortType": "",
    "isHLtitle": "true",
}


def _to_window_date(dt: datetime) -> str:
    return dt.astimezone(TIMEZONE).date().isoformat()


def parse_cninfo_payload(payload: dict, fetched_at: datetime) -> list[SourceItem]:
    """public API's announcementType codes have no verified official label map
    (announcementTypeName is null in responses); classification must use the
    rule engine over the Chinese title, never a fabricated code->name map."""
    items: list[SourceItem] = []
    for row in payload.get("announcements", []):
        announcement_id = row.get("announcementId")
        raw_time = row.get("announcementTime")
        adjunct_url = row.get("adjunctUrl") or ""
        title = str(row.get("announcementTitle") or row.get("shortTitle") or "").strip()
        if not title:
            continue
        if raw_time is None or announcement_id is None:
            continue
        if not str(adjunct_url).lower().endswith(".pdf"):
            continue
        try:
            published_at = datetime.fromtimestamp(
                int(raw_time) / 1000, tz=TIMEZONE
            )
        except (ValueError, OSError, OverflowError):
            continue
        sec_name = str(row.get("secName") or "").strip()
        items.append(
            SourceItem(
                external_id=str(announcement_id),
                title=title,
                publisher=PUBLISHER,
                published_at=published_at,
                original_url=urljoin(STATIC_PREFIX, adjunct_url),
                summary="",
                keywords=(sec_name,) if sec_name else (),
                fetched_at=fetched_at,
            )
        )
    return items


class CninfoAdapter:
    """巨潮资讯公告 JSON 适配器（免登录公开接口，沪深北 A 股法定披露）。

    cursor 为公告日期（Asia/Shanghai, YYYY-MM-DD）；窗口 = [cursor, today]，
    只在列、分页间限速（默认 1.5s）；沪市全市场接口返回 0，需股票维度查询，
    故默认只取 szse（深市全市场）与 bj（北交所）。
    """

    def __init__(
        self,
        query_url: str = QUERY_URL,
        columns: tuple[str, ...] = DEFAULT_COLUMNS,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
        request_interval: float = REQUEST_INTERVAL,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.query_url = query_url
        self.columns = tuple(columns)
        self.page_size = page_size
        self.max_pages = max_pages
        self.request_interval = request_interval
        self.user_agent = user_agent

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        today = _to_window_date(now)
        start = (cursor or today).strip() or today
        if start > today:
            return FetchBatch(items=[], new_cursor=cursor or "")
        window = f"{start}~{today}"

        items: list[SourceItem] = []
        sent_any_request = False
        for column in self.columns:
            for page in range(1, self.max_pages + 1):
                if sent_any_request:
                    time.sleep(self.request_interval)
                rows = self._request_column_page(column, page, window, now)
                sent_any_request = True
                items.extend(rows)
                if len(rows) < self.page_size:
                    break

        kept = _filter_after(items, cursor)
        return FetchBatch(
            items=kept,
            new_cursor=_max_date(kept) or (cursor or ""),
        )

    def _request_column_page(
        self, column: str, page: int, window: str, now: datetime
    ) -> list[SourceItem]:
        form = dict(_FORM_BASE)
        form.update(
            {
                "pageNum": str(page),
                "pageSize": str(self.page_size),
                "column": column,
                "seDate": window,
            }
        )
        body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(
            self.query_url,
            data=body,
            method="POST",
            headers={
                "User-Agent": self.user_agent,
                "Origin": "https://www.cninfo.com.cn",
                "Referer": "https://www.cninfo.com.cn/new/disclosure",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                response_body = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            raise SourceFetchError(f"巨潮资讯接口返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"巨潮资讯接口不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"巨潮资讯接口返回 {status}")
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceFetchError(f"巨潮资讯接口返回非法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceFetchError("巨潮资讯接口返回非法 JSON 结构")
        return parse_cninfo_payload(payload, fetched_at=now)


def _filter_after(items: list[SourceItem], cursor: str | None) -> list[SourceItem]:
    """窗口下界用 >=（含 cursor 当日）：允许同日重复消费，
    已入库条目靠 UNIQUE(source_id, external_id) 去重兜底，
    否则会永久丢失 cursor 当日 09:00 后发布的公告。"""
    marker = (cursor or "").strip()
    if not marker:
        return items
    return [item for item in items if _to_window_date(item.published_at) >= marker]


def _max_date(items: list[SourceItem]) -> str:
    if not items:
        return ""
    return max(_to_window_date(item.published_at) for item in items)
