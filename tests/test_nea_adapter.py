import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)

SAMPLE_PAYLOAD = {
    "datasource": [
        {
            "contentId": "20260821f3fa895ecd23480a83547ca29b12f4cc",
            "title": "新型储能成为第一大调节性资源",
            "publishTime": "2026-08-21 17:37:04",
            "publishUrl": "../20260821/f3fa895ecd23480a83547ca29b12f4cc/c.html",
            "summary": "截至6月底全国新型储能装机达1.53亿千瓦。",
            "keywords": "储能,新型储能,规模化",
            "contentType": "News",
        },
        {
            "contentId": "20260822abcd",
            "title": "<a href=\"http://example.com/rerouted\"><span>某外链条目</span></a>",
            "publishTime": "2026-08-22 09:00:00",
            "publishUrl": "../20260822/abcd/c.html",
            "summary": "",
            "keywords": "",
            "contentType": "Link",
        },
        {
            "contentId": "20260819old",
            "title": "一条旧闻",
            "publishTime": "2026-08-19 08:00:00",
            "publishUrl": "../20260819/old/c.html",
            "summary": "旧。</a",
            "keywords": None,
            "contentType": "News",
        },
    ]
}


def test_parse_nea_payload_normalizes_urls_and_times() -> None:
    from intel_daily.adapters.nea import parse_nea_payload

    items = parse_nea_payload(
        SAMPLE_PAYLOAD,
        base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
        fetched_at=NOW,
    )

    first = items[0]
    assert first.external_id == "20260821f3fa895ecd23480a83547ca29b12f4cc"
    assert (
        first.original_url
        == "http://www.nea.gov.cn/20260821/f3fa895ecd23480a83547ca29b12f4cc/c.html"
    )
    assert first.published_at == datetime(
        2026, 8, 21, 17, 37, 4, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert first.title == "新型储能成为第一大调节性资源"
    assert first.summary == "截至6月底全国新型储能装机达1.53亿千瓦。"
    assert first.keywords == ("储能", "新型储能", "规模化")

    link_item = items[1]
    assert link_item.original_url == "http://example.com/rerouted"
    assert link_item.title == "某外链条目"


def test_parse_nea_payload_ignores_date_article_urls() -> None:
    """2022-05/07 老式 URL（c_1310587396.htm）不因相对路径而破坏。"""
    from intel_daily.adapters.nea import parse_nea_payload

    payload = {
        "datasource": [
            {
                "contentId": "legacy-1",
                "title": "旧系统新闻",
                "publishTime": "2022-05-07 19:12:12",
                "publishUrl": "../2022-05/07/c_1310587396.htm",
                "summary": "旧。</a",
                "keywords": [],
                "contentType": "News",
            }
        ]
    }
    items = parse_nea_payload(
        payload, base_url="http://www.nea.gov.cn/xwzx/nyyw.htm", fetched_at=NOW
    )
    assert items[0].original_url == "http://www.nea.gov.cn/2022-05/07/c_1310587396.htm"


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {}
    status: int = 200

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, payload: dict, status: int = 200) -> None:
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
        return f"http://127.0.0.1:{self.port}/list.json"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_nea_fetch_returns_batch_and_advances_cursor() -> None:
    from intel_daily.adapters.nea import NeaAdapter

    with _Server(SAMPLE_PAYLOAD) as server:
        batch = NeaAdapter(
            list_url=server.url, user_agent="intel-daily/0.1"
        ).fetch(cursor="2026-08-20 00:00:00", now=NOW)

    assert len(batch.items) == 2
    assert batch.new_cursor == "2026-08-22 09:00:00"
    assert [item.published_at.isoformat() for item in batch.items] == [
        datetime(2026, 8, 21, 17, 37, 4, tzinfo=ZoneInfo("Asia/Shanghai")).isoformat(),
        datetime(2026, 8, 22, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).isoformat(),
    ]


def test_nea_fetch_keeps_cursor_when_nothing_new() -> None:
    from intel_daily.adapters.nea import NeaAdapter

    old_only = {"datasource": [SAMPLE_PAYLOAD["datasource"][2]]}
    with _Server(old_only) as server:
        batch = NeaAdapter(
            list_url=server.url, user_agent="intel-daily/0.1"
        ).fetch(cursor="2026-08-22 09:00:00", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "2026-08-22 09:00:00"


def test_nea_fetch_raises_on_non_200_and_bad_json() -> None:
    from intel_daily.adapters.nea import NeaAdapter, SourceFetchError

    with _Server(SAMPLE_PAYLOAD, status=500) as server:
        with pytest.raises(SourceFetchError):
            NeaAdapter(
                list_url=server.url, user_agent="intel-daily/0.1"
            ).fetch(cursor=None, now=NOW)

    with _Server({"datasource": []}) as server:
        with pytest.raises(SourceFetchError):
            NeaAdapter(
                list_url="http://127.0.0.1:1/list.json", user_agent="intel-daily/0.1"
            ).fetch(cursor=None, now=NOW)
