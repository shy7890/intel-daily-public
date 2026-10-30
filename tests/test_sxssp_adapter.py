import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)

SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>碳酸锂报价-生意社</title></head>
<body>
<div class="tsl-crumb">当前位置：碳酸锂频道</div>
<div class="newslist">
<ul>
<li><span class="list-time">2026-08-26</span><a href="/news/detail-20260826-95201.html">8月26日生意社碳酸锂（电池级）基准价为85500元/吨</a></li>
<li><a href="https://tsl.100ppi.com/news/detail-20260825-95198.html">2026年8月25日生意社碳酸锂（电池级）基准价为86000元/吨</a></li>
<li><span class="list-time">2026-08-24</span><a href="/news/detail-20260824-95102.html">8月24日生意社碳酸锂（电池级）基准价为86500元/吨</a></li>
<li><span class="list-time">2026-08-26</span><a href="/news/detail-20260826-84210.html">新能源车市动态：宁德时代发布新一代动力电池</a></li>
<li><a href="/news/detail-20260826-84211.html">生意社碳酸锂基准价（无日期坏条目）</a></li>
</ul>
</div>
</body>
</html>
"""


def parse_payload(html: str, page_url: str = "https://tsl.100ppi.com/"):
    from intel_daily.adapters.sxssp import parse_sxssp_payload

    return parse_sxssp_payload(html, page_url=page_url, fetched_at=NOW)


def test_parse_sxssp_payload_builds_items() -> None:
    items = parse_payload(SAMPLE_HTML)

    assert len(items) == 3

    first = items[0]
    assert first.external_id == "2026-08-26-detail-20260826-95201"
    assert first.title == "8月26日生意社碳酸锂（电池级）基准价为85500元/吨"
    assert first.publisher == "生意社"
    assert first.published_at == datetime(
        2026, 8, 26, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert first.original_url == "https://tsl.100ppi.com/news/detail-20260826-95201.html"
    assert first.summary == ""
    assert first.keywords == ("碳酸锂",)
    assert first.fetched_at == NOW


def test_parse_sxssp_payload_absolutizes_and_keeps_absolute_links() -> None:
    items = parse_payload(SAMPLE_HTML)

    by_id = {item.external_id: item for item in items}
    assert by_id["2026-08-25-detail-20260825-95198"].original_url == (
        "https://tsl.100ppi.com/news/detail-20260825-95198.html"
    )


def test_parse_sxssp_payload_supports_chinese_date_and_skips_non_bench_and_bad() -> None:
    items = parse_payload(SAMPLE_HTML)

    assert items[1].published_at == datetime(
        2026, 8, 25, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    titles = [item.title for item in items]
    assert not any("宁德时代" in title for title in titles)
    assert not any("无日期" in title for title in titles)


def test_parse_sxssp_payload_returns_empty_for_page_without_list() -> None:
    assert parse_payload("<html><body>暂无数据</body></html>") == []


class _Handler(BaseHTTPRequestHandler):
    payload: str = ""
    status: int = 200

    def do_GET(self) -> None:  # noqa: N802
        body = self.payload.encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, payload: str, status: int = 200) -> None:
        handler = type(
            "BoundHandler", (_Handler,), {"payload": payload, "status": status}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server):
    from intel_daily.adapters.sxssp import SxsspAdapter

    return SxsspAdapter(
        list_url=server.url,
        user_agent="intel-daily/0.1",
    )


def test_sxssp_fetch_filters_by_cursor_and_advances() -> None:
    with _Server(SAMPLE_HTML) as server:
        batch = make_adapter(server).fetch(cursor="2026-08-25", now=NOW)

    assert [item.external_id for item in batch.items] == ["2026-08-26-detail-20260826-95201"]
    assert batch.new_cursor == "2026-08-26"


def test_sxssp_fetch_first_run_takes_everything() -> None:
    with _Server(SAMPLE_HTML) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert len(batch.items) == 3
    assert batch.new_cursor == "2026-08-26"


def test_sxssp_fetch_keeps_cursor_when_nothing_new() -> None:
    with _Server(SAMPLE_HTML) as server:
        batch = make_adapter(server).fetch(cursor="2026-08-26", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "2026-08-26"


def test_sxssp_fetch_raises_on_non_200_unreachable_and_challenge() -> None:
    from intel_daily.adapters.nea import SourceFetchError
    from intel_daily.adapters.sxssp import SxsspAdapter

    with _Server(SAMPLE_HTML, status=500) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with pytest.raises(SourceFetchError):
        SxsspAdapter(
            list_url="http://127.0.0.1:1/",
            user_agent="intel-daily/0.1",
        ).fetch(cursor=None, now=NOW)

    challenge = "<html><body><p>正在进行安全检查，请稍候...</p></body></html>"
    with _Server(challenge) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)
