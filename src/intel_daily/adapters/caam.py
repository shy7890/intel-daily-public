import html
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

DEFAULT_LIST_URL = "http://www.caam.org.cn/tjsj"
PUBLISHER = "中国汽车工业协会"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20
REQUEST_INTERVAL = 1.5
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_ARTICLE_DATE_FORMAT = "%Y-%m-%d"
_LI_RE = re.compile(r"<li\b[^>]*>(?P<body>.*?)</li>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*?)>(?P<inner>.*?)</a>", re.IGNORECASE | re.DOTALL
)
_HREF_RE = re.compile(
    r'href\s*=\s*["\'](?P<href>[^"\']+)["\']', re.IGNORECASE
)
_ARTICLE_ID_RE = re.compile(r"con_(\d+)\.html", re.IGNORECASE)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parse_article_date(raw: str) -> datetime | None:
    match = _DATE_RE.search(raw)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), _ARTICLE_DATE_FORMAT).replace(
            tzinfo=TIMEZONE
        )
    except ValueError:
        return None


def _article_links(
    chunk: str, base_url: str, fetched_at: datetime
) -> list[SourceItem]:
    chunk_date = _parse_article_date(chunk)
    items: list[SourceItem] = []
    for anchor in _ANCHOR_RE.finditer(chunk):
        href = _HREF_RE.search(anchor.group("attrs"))
        if href is None:
            continue
        article_url = href.group("href")
        id_match = _ARTICLE_ID_RE.search(article_url)
        if id_match is None:
            continue
        title = _strip_markup(anchor.group("inner")).strip("。· \t\r\n")
        if not title.strip():
            continue
        items.append(
            SourceItem(
                external_id=id_match.group(1),
                title=title,
                publisher=PUBLISHER,
                published_at=chunk_date or fetched_at,
                original_url=urljoin(base_url, article_url),
                summary="",
                keywords=tuple(),
                fetched_at=fetched_at,
            )
        )
    return items


def parse_caam_page(
    page_html: str,
    fetched_at: datetime,
    base_url: str = DEFAULT_LIST_URL,
) -> list[SourceItem]:
    """中汽协统计数据栏目列表解析（一期只做「列表发现」：标题+原文链接+日期）。

    按 `<li>` 列表项块逐块解析文章链接（`/chn/<cate>/cate_<n>/con_<id>.html` 形态）；
    列表项无日期时 published_at 兜底为 fetched_at（轮询发现日期）。
    若 li 块无任何文章链接而整页存在文章链接（真实结构并非 li 容器），
    则退化到整页发现（日期一律兜底）；无命中返回空列表。
    """
    items: list[SourceItem] = []
    for chunk in _LI_RE.findall(page_html):
        items.extend(_article_links(chunk, base_url, fetched_at))
    if items:
        return items
    return (
        _article_links(page_html, base_url, fetched_at)
        if _ARTICLE_ID_RE.search(page_html)
        else []
    )


def _decode_page(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _slice_by_cursor(
    items: list[SourceItem], cursor: str | None
) -> tuple[list[SourceItem], str]:
    marker = (cursor or "").strip()
    if not items:
        return [], marker
    if not marker:
        return items, items[0].external_id
    for index, item in enumerate(items):
        if item.external_id == marker:
            if index:
                return items[:index], items[0].external_id
            return [], marker
    return items, items[0].external_id


class CaamAdapter:
    """中汽协「统计数据」栏目 HTML 适配器（一手协会源、月频列表发现）。

    列表有滞后（实测停在 2026年5月）：以「cursor=列表顶部文章 external_id」的
    URL 级游标容忍滞后，重复文章由 items 表 UNIQUE(source_id, external_id) 与
    指纹去重承担；每天轮询 1 次；浏览器级 UA/Referer 头（裸 UA 可能 403）。
    不抓正文、不追翻页；`<li>` 容器与样例有出入时按「仅列表发现」边界兜底。
    """

    def __init__(
        self,
        list_url: str = DEFAULT_LIST_URL,
        request_interval: float = REQUEST_INTERVAL,
        user_agent: str = BROWSER_USER_AGENT,
    ) -> None:
        self.list_url = list_url
        self.request_interval = request_interval
        self.user_agent = user_agent

    def fetch(self, cursor: str | None, now: datetime) -> FetchBatch:
        time.sleep(self.request_interval)
        request = urllib.request.Request(
            self.list_url,
            headers={
                "User-Agent": self.user_agent,
                "Referer": self.list_url,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT
            ) as response:
                body = response.read()
                status = getattr(response, "status", 200)
                charset = response.headers.get_content_charset()
        except urllib.error.HTTPError as exc:
            raise SourceFetchError(
                f"中汽协统计数据栏目返回 {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"中汽协统计数据栏目不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"中汽协统计数据栏目返回 {status}")

        items = parse_caam_page(
            _decode_page(body, charset), fetched_at=now, base_url=self.list_url
        )
        new_items, new_cursor = _slice_by_cursor(items, cursor)
        return FetchBatch(items=new_items, new_cursor=new_cursor)
