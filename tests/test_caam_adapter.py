import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)

# 内嵌固定样本：按「2026-08-27 实测形态」复制——CAAM tjsj 栏目的文章 URL
# 形态（/chn/4/cate_32/con_<id>.html）与标题形态（"2026年5月新能源汽车产销
# 情况简析"）；列表项容器（<li> + 日期 span）为通用结构假设，日期/标签细节
# 可能与真实页面有出入（见实现注记：无命中即返回空列表，不做猜测乱构）。
SAMPLE_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>中国汽车工业协会 统计数据</title>
</head>
<body>
<div id="nav">
  <a href="http://www.caam.org.cn/">首页</a>
  <a href="/chn/4/cate_32.htm">统计数据</a>
</div>
<div class="list_wrap">
  <ul class="tjjd_list">
    <li><a href="/chn/4/cate_32/con_5237224.html" target="_blank">2026年5月新能源汽车产销情况简析</a><span class="date">2026-06-09</span></li>
    <li><a href="/chn/4/cate_32/con_5237223.html">2026年5月乘用车产销情况简析</a><span class="date">2026-06-09</span></li>
    <li><a href="/chn/4/cate_32/con_5237222.html"><span>2026年5月商用车产销情况简析</span></a></li>
    <li><a href="/chn/4/cate_32/con_abc.html">格式异常的文章链接</a></li>
    <li><a href="http://www.example.com/report.html">外部网站报告</a></li>
    <li><a href="javascript:void(0)">下一页</a></li>
    <li><span class="date">2026-06-09</span></li>
  </ul>
  <div class="pager"><a href="/chn/4/cate_32_2.html">下一页</a></div>
</div>
</body>
</html>
"""


def parse_page(page_html: str, fetched_at: datetime = NOW):
    from intel_daily.adapters.caam import parse_caam_page

    return parse_caam_page(page_html, fetched_at=fetched_at)


def test_parse_caam_page_extracts_title_url_and_external_id() -> None:
    items = parse_page(SAMPLE_PAGE)

    assert len(items) == 3
    first = items[0]
    assert first.external_id == "5237224"
    assert first.title == "2026年5月新能源汽车产销情况简析"
    assert first.publisher == "中国汽车工业协会"
    assert (
        first.original_url
        == "http://www.caam.org.cn/chn/4/cate_32/con_5237224.html"
    )
    assert first.published_at == datetime(
        2026, 6, 9, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert first.summary == ""
    assert first.keywords == tuple()
    assert first.fetched_at == NOW

    second = items[1]
    assert second.external_id == "5237223"
    assert second.title == "2026年5月乘用车产销情况简析"
    assert items[2].title == "2026年5月商用车产销情况简析"


def test_parse_caam_page_skips_non_article_and_malformed_rows() -> None:
    items = parse_page(SAMPLE_PAGE)

    titles = [item.title for item in items]
    assert "格式异常的文章链接" not in titles
    assert "外部网站报告" not in titles
    assert len(items) == 3


def test_parse_caam_page_falls_back_to_fetched_at_when_no_date() -> None:
    items = parse_page(SAMPLE_PAGE)

    assert items[2].external_id == "5237222"
    assert items[2].published_at == NOW


def test_parse_caam_page_empty_or_no_article_links_returns_empty() -> None:
    assert parse_page("") == []
    noise = '<html><body><a href="/chn/4/cate_32.htm">统计数据</a></body></html>'
    assert parse_page(noise) == []


def test_parse_caam_page_discovery_fallback_without_li_structure() -> None:
    """真实页面若不用 <li> 容器，仍按文章 URL 模式做列表发现（日期一律兜底）。"""
    page = (
        '<div class="list"><a href="/chn/4/cate_32/con_5237224.html">'
        "2026年5月新能源汽车产销情况简析</a></div>"
    )
    items = parse_page(page)

    assert len(items) == 1
    assert items[0].external_id == "5237224"
    assert items[0].published_at == NOW


def test_decode_page_falls_back_to_gb18030() -> None:
    from intel_daily.adapters.caam import _decode_page

    expected = "2026年5月新能源汽车产销情况简析"
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
        return f"http://127.0.0.1:{self.port}/tjsj"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server):
    from intel_daily.adapters.caam import CaamAdapter

    return CaamAdapter(
        list_url=server.url, request_interval=0, user_agent="intel-daily/0.1"
    )


def test_caam_fetch_first_run_returns_all_and_advances_cursor() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert [item.external_id for item in batch.items] == [
        "5237224",
        "5237223",
        "5237222",
    ]
    assert batch.new_cursor == "5237224"


def test_caam_fetch_returns_only_items_above_known_cursor() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="5237223", now=NOW)

    assert [item.external_id for item in batch.items] == ["5237224"]
    assert batch.new_cursor == "5237224"


def test_caam_fetch_keeps_cursor_when_nothing_new() -> None:
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="5237224", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "5237224"


def test_caam_fetch_cursor_not_found_returns_all_items() -> None:
    """列表滞后/重排：旧 cursor 不在新列表中 → 全量返回（DB 去重吸收），cursor 前移。"""
    with _Server(SAMPLE_PAGE) as server:
        batch = make_adapter(server).fetch(cursor="5237229", now=NOW)

    assert len(batch.items) == 3
    assert batch.new_cursor == "5237224"


def test_caam_fetch_empty_page_keeps_cursor() -> None:
    with _Server("<html><body>临时空页</body></html>") as server:
        batch = make_adapter(server).fetch(cursor="5237224", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "5237224"


def test_caam_fetch_sends_browser_headers() -> None:
    with _Server(SAMPLE_PAGE) as server:
        make_adapter(server).fetch(cursor=None, now=NOW)

    headers = _Handler.last_headers
    assert headers.get("user-agent") == "intel-daily/0.1"
    assert headers.get("referer") == server.url
    assert "text/html" in headers.get("accept", "")


def test_caam_fetch_raises_on_non_200_and_unreachable() -> None:
    from intel_daily.adapters.caam import CaamAdapter
    from intel_daily.adapters.nea import SourceFetchError

    with _Server(SAMPLE_PAGE, status=404) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with _Server(SAMPLE_PAGE, status=500) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with pytest.raises(SourceFetchError):
        CaamAdapter(
            list_url="http://127.0.0.1:1/tjsj",
            request_interval=0,
        ).fetch(cursor=None, now=NOW)


def test_parse_strips_list_dot_prefix_from_title() -> None:
    """标题中实测残留的“·+换行”列表前缀（真实页面 2026-08-27 抽检发现）应被清除。"""
    from intel_daily.adapters.caam import parse_caam_page

    fetched = datetime(2026, 8, 27, tzinfo=timezone.utc)
    html = (
        '<!doctype html><ul>'
        '<li><span>2026-05-11</span> · \n\t\t<a href="/chn/4/cate_32/con_111.html">'
        '·\n\t\t2026年5月新能源汽车产销情况简析</a></li>'
        "</ul>"
    )
    items = parse_caam_page(html, fetched)
    assert items[0].title == "2026年5月新能源汽车产销情况简析"
    assert items[0].title.startswith("·") is False
