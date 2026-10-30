import json
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)
SH_TZ = ZoneInfo("Asia/Shanghai")

SAMPLE_PAYLOAD = {
    "code": "0",
    "msg": "接口访问成功！",
    "param": {"trade_date": ["20260826"], "trade_type": ["0"], "variety": ["lc"]},
    "data": [
        {
            "variety": "总计",
            "varietyEn": "Total",
            "varietyOrder": "",
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "clearPrice": None,
            "lastClear": None,
            "diff": None,
            "volumn": 313449,
            "openInterest": 681229,
        },
        {
            "variety": "碳酸锂",
            "varietyEn": "Lithium Carbonate",
            "varietyOrder": "lc",
            "delivMonth": "2611",
            "open": 149800,
            "high": 154220,
            "low": 147680,
            "close": 151200,
            "clearPrice": 151620,
            "lastClear": 153100,
            "diff": -1900,
            "volumn": 44245,
            "openInterest": 141379,
        },
        {
            "variety": "碳酸锂",
            "varietyEn": "Lithium Carbonate",
            "varietyOrder": "lc",
            "delivMonth": "2701",
            "open": 150980,
            "high": 154940,
            "low": 148320,
            "close": 152160,
            "clearPrice": 152200,
            "lastClear": 153960,
            "diff": -1800,
            "volumn": 217163,
            "openInterest": 358778,
        },
        {
            "variety": "碳酸锂小计",
            "varietyEn": "Lithium Carbonate SubTotal",
            "varietyOrder": "lc",
            "volumn": 313449,
            "openInterest": 681229,
        },
    ],
    "time": 1787809786311,
}

NOT_READY_PAYLOAD = {
    "code": "0",
    "msg": "接口访问成功！",
    "data": [
        {
            "variety": "总计",
            "varietyEn": "Total",
            "varietyOrder": "",
            "volumn": 0,
            "openInterest": 0,
        }
    ],
    "time": 1787809786311,
}


def parse_payload(payload: dict):
    from intel_daily.adapters.gfex import parse_gfex_payload

    return parse_gfex_payload(payload, trade_date="20260826", fetched_at=NOW)


def test_parse_gfex_payload_builds_lithium_item() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "20260826-lc-2701"
    assert item.title == "广期所 碳酸锂：主力合约结算价 152200（2026-08-26）"
    assert item.publisher == "广州期货交易所"
    assert item.published_at == datetime(2026, 8, 26, 0, 0, 0, tzinfo=SH_TZ)
    assert item.original_url.endswith("/gfex/sspzb/sspz.shtml")
    assert item.keywords == ("碳酸锂",)
    assert "2701" in item.summary
    assert "结算 152200" in item.summary
    assert "持仓 358778" in item.summary


def test_parse_gfex_payload_main_contract_is_max_open_interest() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)
    assert items[0].summary.count("主力") == 1
    assert "2611" not in items[0].external_id


def test_parse_gfex_payload_empty_when_no_contract_rows() -> None:
    assert parse_payload(NOT_READY_PAYLOAD) == []


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    last_form: dict = {}

    def _respond(self) -> None:
        status, payload = self.routes.get(
            _Handler.last_form.get("trade_date", ""), (404, {})
        )
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        _Handler.last_form = dict(urllib.parse.parse_qsl(raw))
        self._respond()

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, routes: dict[str, tuple[int, object]] | None = None) -> None:
        self.routes = routes or {}

    def __enter__(self) -> "_Server":
        self.handler = type(
            "BoundHandler", (_Handler,), {"routes": self.routes}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/u/interfacesWebTiDayQuotes/loadList"

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server, search_days: int = 2):
    from intel_daily.adapters.gfex import GfexAdapter

    return GfexAdapter(
        query_url=server.url,
        varieties=("lc",),
        search_days=search_days,
        user_agent="intel-daily/0.1",
    )


def test_gfex_fetch_sends_form_for_previous_closed_day() -> None:
    routes = {
        "20260826": (200, SAMPLE_PAYLOAD),
        "20260825": (200, SAMPLE_PAYLOAD),
    }
    with _Server(routes) as server:
        make_adapter(server).fetch(cursor=None, now=NOW)

    form = _Handler.last_form
    assert form["trade_date"] == "20260826"
    assert form["trade_type"] == "0"
    assert form["variety"] == "lc"


def test_gfex_fetch_returns_items_and_advances_cursor() -> None:
    with _Server({"20260826": (200, SAMPLE_PAYLOAD)}) as server:
        batch = make_adapter(server).fetch(cursor="20260825", now=NOW)

    assert len(batch.items) == 1
    assert batch.new_cursor == "20260826"
    assert batch.items[0].external_id == "20260826-lc-2701"


def test_gfex_fetch_keeps_cursor_when_date_not_new() -> None:
    with _Server({"20260826": (200, SAMPLE_PAYLOAD)}) as server:
        batch = make_adapter(server).fetch(cursor="20260826", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "20260826"


def test_gfex_fetch_skips_not_ready_today_and_uses_older_closed_date() -> None:
    routes = {
        "20260826": (200, NOT_READY_PAYLOAD),
        "20260825": (200, SAMPLE_PAYLOAD),
    }
    with _Server(routes) as server:
        batch = make_adapter(server, search_days=3).fetch(cursor="20260824", now=NOW)

    assert len(batch.items) == 1
    assert batch.new_cursor == "20260825"


def test_gfex_fetch_raises_when_all_days_unavailable() -> None:
    from intel_daily.adapters.nea import SourceFetchError

    with _Server({"20260826": (404, {})}) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server, search_days=2).fetch(cursor=None, now=NOW)


def test_gfex_fetch_raises_on_http_500() -> None:
    from intel_daily.adapters.nea import SourceFetchError

    with _Server({"20260826": (500, {})}) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server, search_days=2).fetch(cursor=None, now=NOW)


def test_gfex_fetch_raises_on_unreachable_server() -> None:
    from intel_daily.adapters.nea import SourceFetchError
    from intel_daily.adapters.gfex import GfexAdapter

    adapter = GfexAdapter(
        query_url="http://127.0.0.1:1/u/interfacesWebTiDayQuotes/loadList",
        search_days=2,
        user_agent="intel-daily/0.1",
    )
    with pytest.raises(SourceFetchError):
        adapter.fetch(cursor=None, now=NOW)
