import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)

# 内嵌固定样本：取自 2026-08-27 真实抓取
# （`https://www.cpcaauto.com/news.php?types=bgzl&anid=126`，HTTP 200，15.3KB，
# 全页 173 条记录，分页 9 页；此处收缩为前 3 条「新能源月报」文章 + 导航/侧栏/
# 翻页噪声行，行形态（<li class="q"> + <span>日期</span> + <a href="newslist.php
# ?types=bgzl&id=<id>">&nbsp;&nbsp;【新能源】…）与原页逐字一致，仅删减顶部行数）。
SAMPLE_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>中国汽车流通协会乘用车市场信息联席分会</title>
</head>
<body>
<div class="nav warp">
  <ul id="navtop">
    <li class="mainlevel"><a class="main_color colored" href="/"><b>首页</b></a></li>
    <li class="mainlevel"><a class="main_color colored" href="news.php?types=news"><b>行业新闻</b></a></li>
    <li class="mainlevel active"><a class="main_color colored" href="news.php?types=bgzl"><b>发布会报告</b></a></li>
    <li class="mainlevel"><a class="main_color colored" href="about.php?id=2">协会简介</a></li>
  </ul>
</div>
<div class="warp clearfix">
  <div class="left fl">
    <div class="list_d">
      <ul>
            <li class="q">
              <span>2026-08-06 </span>
              <a href="newslist.php?types=bgzl&id=1517">&nbsp;&nbsp;【新能源】2026年6月新能源汽车行业月报</a>
            </li>
            <li class="q">
              <span>2026-07-07 </span>
              <a href="newslist.php?types=bgzl&id=1509">&nbsp;&nbsp;【新能源】2026年5月新能源汽车行业月报</a>
            </li>
            <li class="q">
              <a href="newslist.php?types=bgzl&id=1501">【新能源】2026年4月新能源汽车行业月报</a>
            </li>
            <li class="q">
              <span>2026-06-04 </span>
              <a href="javascript:void(0)">下一页</a>
            </li>
      </ul>
    </div>
    <div class="pagebar">
      <form name="selectPageForm" action="/news.php" method="get">
        <div id="pager" class="pagebar">
          <span class="f_l">总计 <b>173</b>  个记录</span>
          <a href="">首 页</a>
          <a href="news.php?types=bgzl&anid=126&nid=&page=2">2</a>
          <a class="next" href="news.php?types=bgzl&anid=126&nid=&page=2">下一页</a>
        </div>
      </form>
    </div>
  </div>
  <div class="right fr">
    <div class="cat_list">
      <h3>发布会报告</h3>
      <ul>
        <li><a href="news.php?types=bgzl&anid=126">新能源月报</a></li>
      </ul>
    </div>
  </div>
</div>
</body>
</html>
"""


def parse_page(page_html: str, fetched_at: datetime = NOW):
    from intel_daily.adapters.cpca import _cpca_articles

    return _cpca_articles(page_html, fetched_at=fetched_at)


def test_parse_cpca_page_extracts_title_url_and_external_id() -> None:
    items = parse_page(SAMPLE_PAGE)

    assert len(items) == 3
    first = items[0]
    assert first.external_id == "1517"
    assert first.title == "【新能源】2026年6月新能源汽车行业月报"
    assert first.publisher == "中国汽车流通协会乘用车市场信息联席分会（CPCA）"
    assert (
        first.original_url
        == "https://www.cpcaauto.com/newslist.php?types=bgzl&id=1517"
    )
    assert first.published_at == datetime(
        2026, 8, 6, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert first.summary == ""
    assert first.keywords == tuple()
    assert first.fetched_at == NOW

    assert items[1].external_id == "1509"
    assert items[1].title == "【新能源】2026年5月新能源汽车行业月报"


def test_parse_cpca_page_strips_whitespace_prefix_from_title() -> None:
    """真实页面标题带 &nbsp;&nbsp; 前缀（整理后为 \xa0），应被清除。"""
    items = parse_page(SAMPLE_PAGE)

    assert items[0].title == "【新能源】2026年6月新能源汽车行业月报"
    assert all("\xa0" not in item.title for item in items)
    assert all(item.title == item.title.strip() for item in items)


def test_parse_cpca_page_rejects_malformed_and_nav_rows() -> None:
    """非 newslist.php?id= 链接（导航/侧栏/翻页/about.php?id=/javascript:）一律跳过。"""
    items = parse_page(SAMPLE_PAGE)

    titles = [item.title for item in items]
    assert "页面" not in titles
    assert len(items) == 3
    assert all(not item.title.startswith("·") for item in items)


def test_parse_cpca_page_falls_back_to_fetched_at_when_no_date() -> None:
    items = parse_page(SAMPLE_PAGE)

    assert items[2].external_id == "1501"
    assert items[2].published_at == NOW


def test_parse_cpca_page_empty_or_no_article_links_returns_empty() -> None:
    assert parse_page("") == []
    noise = (
        '<html><body><div class="list_d"><a href="news.php?types=bgzl&anid=126">'
        "新能源月报</a></div></body></html>"
    )
    assert parse_page(noise) == []


def test_parse_cpca_page_discovery_fallback_without_li_structure() -> None:
    """真实页面若不用 <li> 容器，仍按 newslist.php?id= 文章链接模式发现（日期兜底）。"""
    page = (
        '<div id="content"><a href="newslist.php?types=bgzl&id=1517">'
        "【新能源】2026年6月新能源汽车行业月报</a></div>"
    )
    items = parse_page(page)

    assert len(items) == 1
    assert items[0].external_id == "1517"
    assert items[0].published_at == NOW


def test_decode_page_falls_back_to_gb18030() -> None:
    from intel_daily.adapters.cpca import _decode_page

    expected = "【新能源】2026年6月新能源汽车行业月报"
    raw = expected.encode("gb2312")
    assert _decode_page(raw, None) == expected
    assert _decode_page(raw, "gb2312") == expected
    assert _decode_page(expected.encode("utf-8"), "utf-8") == expected


class _Handler(BaseHTTPRequestHandler):
    page: str = SAMPLE_PAGE
    status: int = 200
    charset: str = "utf-8"
    last_headers: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        _Handler.last_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        body = self.page.encode(self.charset)
        self.send_response(self.status)
        self.send_header(
            "Content-Type", f"text/html; charset={self.charset}"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, page: str, status: int = 200) -> None:
        handler = type(
            "BoundHandler", (_Handler,), {"page": page, "status": status}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/news.php?types=bgzl&anid=126"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server):
    from intel_daily.adapters.cpca import CpcaAdapter

    return CpcaAdapter(
        list_url=server.url, request_interval=0, user_agent="intel-daily/0.1"
    )


def test_cpca_fetch_first_run_returns_all_and_advances_cursor() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert [item.external_id for item in batch.items] == [
        "1517",
        "1509",
        "1501",
    ]
    assert batch.new_cursor == "1517"


def test_cpca_fetch_returns_only_items_above_known_cursor() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="1509", now=NOW)

    assert [item.external_id for item in batch.items] == ["1517"]
    assert batch.new_cursor == "1517"


def test_cpca_fetch_keeps_cursor_when_nothing_new() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="1517", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "1517"


def test_cpca_fetch_cursor_not_found_returns_all_items() -> None:
    """列表滞后/重排：旧 cursor 不在新列表中 → 全量返回（DB 去重吸收），cursor 前移。"""
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="99999", now=NOW)

    assert len(batch.items) == 3
    assert batch.new_cursor == "1517"


def test_cpca_fetch_empty_page_keeps_cursor() -> None:
    with _Server("<html><body>临时空页</body></html>") as server:
        batch = make_adapter(server).fetch(cursor="1517", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "1517"


def test_cpca_fetch_sends_full_browser_headers() -> None:
    with _Server(SAMPLE_PAGE) as server:
        make_adapter(server).fetch(cursor=None, now=NOW)

    headers = _Handler.last_headers
    assert headers.get("user-agent") == "intel-daily/0.1"
    assert headers.get("referer") == server.url
    assert "text/html" in headers.get("accept", "")
    assert "zh-CN" in headers.get("accept-language", "")


def test_cpca_fetch_raises_on_non_200_and_unreachable() -> None:
    from intel_daily.adapters.cpca import CpcaAdapter
    from intel_daily.adapters.nea import SourceFetchError

    with _Server(SAMPLE_PAGE, status=404) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with _Server(SAMPLE_PAGE, status=500) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with pytest.raises(SourceFetchError):
        CpcaAdapter(
            list_url="http://127.0.0.1:1/news.php",
            request_interval=0,
        ).fetch(cursor=None, now=NOW)
