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

DEFAULT_LIST_URL = "https://www.cpcaauto.com/news.php?types=bgzl&anid=126"
PUBLISHER = "中国汽车流通协会乘用车市场信息联席分会（CPCA）"
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
_ARTICLE_ID_RE = re.compile(r"(?<![a-z])id=(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(raw: str) -> str:
    """去标签+去实体后，再去掉真实页面残留的 &nbsp;/全角空白及首尾空白。"""
    return html.unescape(_TAG_RE.sub("", raw)).strip(
        " \t\r\n\xa0\u3000\u200b"
    )


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
        # 列表文章链接形态：newslist.php?types=bgzl&id=<id>；
        # 导航/侧栏/翻页为 news.php（含 nid=/page=）或 about.php?id=，一律跳过。
        if "newslist.php" not in article_url:
            continue
        id_match = _ARTICLE_ID_RE.search(article_url)
        if id_match is None:
            continue
        title = _clean_title(anchor.group("inner"))
        if not title:
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


def _cpca_articles(
    page_html: str,
    fetched_at: datetime,
    base_url: str = DEFAULT_LIST_URL,
) -> list[SourceItem]:
    """乘联会「新能源月报」栏目列表解析（一期只做「列表发现」：标题+原文链接+日期）。

    列表行形态（2026-08-27 实测）：`<li class="q"><span>YYYY-MM-DD</span>
    <a href="newslist.php?types=bgzl&id=<id>">…月报</a></li>`；
    行内无日期时 published_at 兜底为 fetched_at（轮询发现日期）。
    若 li 块无任何文章链接而整页存在文章链接（真实结构并非 li 容器），
    则退化到整页发现（日期一律兜底）；无命中返回空列表。
    """
    items: list[SourceItem] = []
    for chunk in _LI_RE.findall(page_html):
        items.extend(_article_links(chunk, base_url, fetched_at))
    if items:
        return items
    if "newslist.php" in page_html and _ARTICLE_ID_RE.search(page_html):
        return _article_links(page_html, base_url, fetched_at)
    return []


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


class CpcaAdapter:
    """乘联会（CPCA）「新能源月报」栏目 HTML 适配器（一手协会源、月频列表发现）。

    正文为图片型长图（29 张无结构化数字），一期只做「发现/标题+链接」，不抓正文、
    不追翻页（首页 20 条覆盖约 20 个月，月频足够通知月刊发布）；以
    「cursor=列表顶部文章 external_id」的 URL 级游标容忍列表滞后，重复文章由
    items 表 UNIQUE(source_id, external_id) 与指纹去重承担；每天轮询 1 次；
    必须带浏览器级 UA + Accept/Accept-Language/Referer 完整头（裸 UA 403）。
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
            raise SourceFetchError(f"乘联会新能源月报栏目返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"乘联会新能源月报栏目不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"乘联会新能源月报栏目返回 {status}")

        items = _cpca_articles(
            _decode_page(body, charset), fetched_at=now, base_url=self.list_url
        )
        new_items, new_cursor = _slice_by_cursor(items, cursor)
        return FetchBatch(items=new_items, new_cursor=new_cursor)
