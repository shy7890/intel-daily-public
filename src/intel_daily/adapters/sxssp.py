import hashlib
import html
import re
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from intel_daily.adapters.contract import FetchBatch, SourceItem
from intel_daily.adapters.nea import SourceFetchError

DEFAULT_LIST_URL = "https://tsl.100ppi.com/"
PUBLISHER = "生意社"
TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = 20
_BENCH_MARK = "基准价"
_DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_PRODUCT_RE = re.compile(r"生意社(.+?)(?:[（(]|基准价)")
_CHALLENGE_MARKERS = ("HW_CHECK", "正在进行安全检查", "安全检查")


class _ListParser(HTMLParser):
    """把页面解析为候选条目：优先取 <li> 内文本与链接；
    整页无 <li> 时退化为逐条 <a>（结构兜底，避免样式差异丢条目）。"""

    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[dict] = []
        self.standalone: list[dict] = []
        self._li: dict | None = None
        self._a: dict | None = None
        self._li_any = False

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "li":
            self._li = {"text": [], "links": []}
            self._li_any = True
        elif tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), "")
            anchor = {"href": href or "", "text": []}
            if self._li is not None:
                self._li["links"].append(anchor)
            else:
                self.standalone.append(anchor)
            self._a = anchor

    def handle_endtag(self, tag) -> None:
        if tag == "a" and self._a is not None:
            if self._li is not None:
                for anchor in self._li["links"][-1:]:
                    if anchor is self._a:
                        self._li["links"] = self._li["links"][:-1] + [anchor]
            self._a = None
        elif tag == "li" and self._li is not None:
            self.candidates.append(self._li)
            self._li = None
        elif tag in ("ul", "ol") and self._li is not None:
            self.candidates.append(self._li)
            self._li = None

    def handle_data(self, data) -> None:
        text = html.unescape(data)
        if self._li is not None:
            self._li["text"].append(text)
        if self._a is not None:
            self._a["text"].append(text)

    def get_candidates(self) -> list[dict]:
        if self._li_any:
            return self.candidates
        return self.standalone


def _extract_date(raw: str) -> datetime | None:
    for pattern in (_DATE_ISO_RE, _DATE_CN_RE):
        match = pattern.search(raw)
        if match:
            year, month, day = (int(part) for part in match.groups())
            try:
                return datetime(year, month, day, tzinfo=TIMEZONE)
            except ValueError:
                return None
    return None


def _extract_keyword(title: str) -> str:
    match = _PRODUCT_RE.search(title)
    name = match.group(1).strip() if match else ""
    return name or "碳酸锂"


def _slug(url: str) -> str:
    segment = urlparse(url).path.rsplit("/", 1)[-1]
    segment = re.sub(r"\.\w+$", "", segment)
    if segment:
        return segment
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def parse_sxssp_payload(html_text: str, page_url: str, fetched_at: datetime) -> list[SourceItem]:
    parser = _ListParser()
    parser.feed(html_text)
    parser.close()

    items: list[SourceItem] = []
    for candidate in parser.get_candidates():
        links = candidate.get("links")
        if not links:
            continue
        anchor = links[0]
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        title = "".join(anchor.get("text", [])).strip()
        if not title:
            continue
        if _BENCH_MARK not in title:
            continue
        item_text = "".join(candidate.get("text", [])) or title
        date = _extract_date(item_text)
        if date is None:
            continue
        items.append(
            SourceItem(
                external_id=f"{date.date().isoformat()}-{_slug(href)}",
                title=title,
                publisher=PUBLISHER,
                published_at=date,
                original_url=urljoin(page_url, href),
                summary="",
                keywords=(_extract_keyword(title),),
                fetched_at=fetched_at,
            )
        )
    return items


def _looks_like_challenge(html_text: str) -> bool:
    return any(marker in html_text for marker in _CHALLENGE_MARKERS)


class SxsspAdapter:
    """生意社碳酸锂频道适配器（频道首页列表，单页无翻页，每日一次）。

    cursor 为日期（Asia/Shanghai, YYYY-MM-DD）；只做标题级抓取，
    价格数字不解析（属于二手/基准价口径线索，后续人工核验）。
    """

    def __init__(
        self,
        list_url: str = DEFAULT_LIST_URL,
        user_agent: str = "intel-daily/0.1 (+internal daily intelligence)",
    ) -> None:
        self.list_url = list_url
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
            raise SourceFetchError(f"生意社碳酸锂频道返回 {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SourceFetchError(f"生意社碳酸锂频道不可达: {exc}") from exc
        if status != 200:
            raise SourceFetchError(f"生意社碳酸锂频道返回 {status}")
        page = body.decode("utf-8", errors="replace")
        if _looks_like_challenge(page):
            raise SourceFetchError("生意社碳酸锂频道返回安全挑战页（HW_CHECK）")

        items = parse_sxssp_payload(page, page_url=self.list_url, fetched_at=now)
        new_items = _filter_after(items, cursor)
        return FetchBatch(
            items=new_items,
            new_cursor=_max_date(items) if new_items else (cursor or ""),
        )


def _filter_after(items: list[SourceItem], cursor: str | None) -> list[SourceItem]:
    marker = (cursor or "").strip()
    if not marker:
        return items
    return [
        item
        for item in items
        if item.published_at.date().isoformat() > marker
    ]


def _max_date(items: list[SourceItem]) -> str:
    if not items:
        return ""
    return max(item.published_at.date().isoformat() for item in items)
