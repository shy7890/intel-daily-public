import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from intel_daily.adapters.nea import SourceFetchError

NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)
SH_TZ = ZoneInfo("Asia/Shanghai")
DAT_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx20260826.dat"

SAMPLE_PAYLOAD = {
    "report_date": "20260826",
    "update_date": "20260826 15:25:10",
    "o_msg": "查询成功",
    "o_curinstrument": [
        {
            "PRODUCTID": "ni_f",
            "PRODUCTNAME": "镍",
            "DELIVERYMONTH": "2610",
            "OPENPRICE": 128750,
            "HIGHESTPRICE": 130050,
            "LOWESTPRICE": 128680,
            "CLOSEPRICE": 129310,
            "SETTLEMENTPRICE": 129520,
            "PRESETTLEMENTPRICE": 129660,
            "ZDI_CHG": None,
            "ZD2_CHG": -140,
            "VOLUME": 150665,
            "TURNOVER": 1951530.112,
            "OPENINTEREST": 127106,
            "OPENINTERESTCHG": 2134,
        },
        {
            "PRODUCTID": "ni_f",
            "PRODUCTNAME": "镍",
            "DELIVERYMONTH": "2611",
            "OPENPRICE": 129610,
            "HIGHESTPRICE": 130320,
            "LOWESTPRICE": 129000,
            "CLOSEPRICE": 129620,
            "SETTLEMENTPRICE": 129770,
            "PRESETTLEMENTPRICE": 129850,
            "ZD2_CHG": -80,
            "VOLUME": 25457,
            "TURNOVER": 330376.62,
            "OPENINTEREST": 68601,
            "OPENINTERESTCHG": 3863,
        },
        {
            "PRODUCTID": "ni_f",
            "PRODUCTNAME": "镍",
            "DELIVERYMONTH": "2699",
            "OPENPRICE": "N/A",
            "HIGHESTPRICE": "",
            "LOWESTPRICE": None,
            "CLOSEPRICE": "",
            "SETTLEMENTPRICE": "",
            "VOLUME": "",
            "TURNOVER": "",
            "OPENINTEREST": "unknown",
            "OPENINTERESTCHG": "",
        },
        {
            "PRODUCTID": "ni_f",
            "PRODUCTNAME": "镍",
            "DELIVERYMONTH": "小计",
            "OPENPRICE": "",
            "HIGHESTPRICE": "",
            "LOWESTPRICE": "",
            "CLOSEPRICE": "",
            "SETTLEMENTPRICE": "",
            "PRESETTLEMENTPRICE": "",
            "ZD2_CHG": "",
            "VOLUME": 242970,
            "TURNOVER": 3147424.726,
            "OPENINTEREST": 306811,
            "OPENINTERESTCHG": -2224,
        },
        {
            "PRODUCTID": "sn_f",
            "PRODUCTNAME": "锡",
            "DELIVERYMONTH": "2610",
            "OPENPRICE": "425980",
            "HIGHESTPRICE": "428940",
            "LOWESTPRICE": "422800",
            "CLOSEPRICE": "425260",
            "SETTLEMENTPRICE": "426090",
            "PRESETTLEMENTPRICE": "426080",
            "ZD2_CHG": 10,
            "VOLUME": "126974",
            "TURNOVER": "5410349.603",
            "OPENINTEREST": "42787",
            "OPENINTERESTCHG": "4053",
        },
        {
            "PRODUCTID": "sn_f",
            "PRODUCTNAME": "锡",
            "DELIVERYMONTH": "小计",
            "OPENPRICE": "",
            "HIGHESTPRICE": "",
            "LOWESTPRICE": "",
            "CLOSEPRICE": "",
            "SETTLEMENTPRICE": "",
            "PRESETTLEMENTPRICE": "",
            "ZD2_CHG": "",
            "VOLUME": "191597",
            "TURNOVER": "8161327.793",
            "OPENINTEREST": 78481,
            "OPENINTERESTCHG": 89,
        },
    ],
}


def parse_payload(payload: dict, dat_url: str = DAT_URL):
    from intel_daily.adapters.shfe import parse_shfe_payload

    return parse_shfe_payload(payload, dat_url=dat_url, fetched_at=NOW)


def test_parse_shfe_payload_builds_one_item_per_product() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)

    assert [item.external_id for item in items] == [
        "20260826-ni_f-2610",
        "20260826-sn_f-2610",
    ]

    nickel = items[0]
    assert nickel.title == "沪期货 镍：主力合约结算价 129520（2026-08-26）"
    assert nickel.publisher == "上海期货交易所"
    assert nickel.published_at == datetime(2026, 8, 26, 15, 25, 10, tzinfo=SH_TZ)
    assert nickel.original_url == DAT_URL
    assert nickel.summary == (
        "主力 2610：开 128750 高 130050 低 128680 收 129310 结算 129520 昨结 129660"
        "；成交 150665 手；持仓 127106（+2134）手"
        "；品种总成交 242970 手、总持仓 306811 手。"
    )
    assert nickel.keywords == ("镍",)
    assert nickel.fetched_at == NOW

    tin = items[1]
    assert tin.title == "沪期货 锡：主力合约结算价 426090（2026-08-26）"
    assert tin.summary == (
        "主力 2610：开 425980 高 428940 低 422800 收 425260 结算 426090 昨结 426080"
        "；成交 126974 手；持仓 42787（+4053）手"
        "；品种总成交 191597 手、总持仓 78481 手。"
    )
    assert tin.keywords == ("锡",)


def test_parse_shfe_payload_ignores_junk_rows_and_excludes_subtotal() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)

    assert [item.external_id for item in items] == [
        "20260826-ni_f-2610",
        "20260826-sn_f-2610",
    ]
    assert items[0].title.startswith("沪期货 镍")


def test_parse_shfe_payload_missing_update_date_falls_back_to_midnight() -> None:
    payload = dict(SAMPLE_PAYLOAD)
    payload["update_date"] = ""

    items = parse_payload(payload)

    assert items[0].published_at == datetime(2026, 8, 26, 0, 0, 0, tzinfo=SH_TZ)


def test_parse_shfe_payload_returns_empty_for_unusable_payload() -> None:
    assert parse_payload({}) == []
    assert parse_payload({"report_date": "20260826"}) == []


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, object]] = {}

    def _respond(self) -> None:
        route = self.routes.get(self.path)
        if not route:
            status, payload = 404, None
        else:
            status, payload = route
        body = b""
        if payload is not None:
            if isinstance(payload, dict):
                body = json.dumps(payload).encode("utf-8")
            else:
                body = payload
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        handler = type("BoundHandler", (_Handler,), {"routes": routes})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/dailydata/"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server, search_days: int = 2):
    from intel_daily.adapters.shfe import ShfeAdapter

    return ShfeAdapter(
        data_base_url=server.url,
        products=("ni_f", "sn_f"),
        search_days=search_days,
        user_agent="intel-daily/0.1",
    )


def test_fetch_returns_new_items_and_advances_cursor() -> None:
    with _Server({"/dailydata/kx20260826.dat": (200, SAMPLE_PAYLOAD)}) as server:
        batch = make_adapter(server).fetch(cursor="20260825", now=NOW)

    assert [item.external_id for item in batch.items] == [
        "20260826-ni_f-2610",
        "20260826-sn_f-2610",
    ]
    assert batch.new_cursor == "20260826"
    assert batch.items[0].original_url.endswith("kx20260826.dat")


def test_fetch_first_run_with_none_cursor() -> None:
    with _Server({"/dailydata/kx20260826.dat": (200, SAMPLE_PAYLOAD)}) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert len(batch.items) == 2
    assert batch.new_cursor == "20260826"


def test_fetch_keeps_cursor_when_file_date_not_new() -> None:
    with _Server({"/dailydata/kx20260826.dat": (200, SAMPLE_PAYLOAD)}) as server:
        batch = make_adapter(server).fetch(cursor="20260826", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "20260826"


def test_fetch_prefers_previous_closed_day_over_intraday_today() -> None:
    today_payload = {
        "report_date": "20260827",
        "update_date": "20260827 13:03:02",
        "o_curinstrument": [
            {
                "PRODUCTID": "ni_f",
                "PRODUCTNAME": "镍",
                "DELIVERYMONTH": "2610",
                "OPENPRICE": "128750",
                "HIGHESTPRICE": "130050",
                "LOWESTPRICE": "128680",
                "CLOSEPRICE": "129310",
                "SETTLEMENTPRICE": "",
                "PRESETTLEMENTPRICE": "",
                "VOLUME": 150665,
                "OPENINTEREST": 127106,
                "OPENINTERESTCHG": 2134,
            },
            {
                "PRODUCTID": "ni_f",
                "PRODUCTNAME": "镍",
                "DELIVERYMONTH": "小计",
                "VOLUME": 242970,
                "OPENINTEREST": 306811,
            },
        ],
    }
    routes = {
        "/dailydata/kx20260827.dat": (200, today_payload),
        "/dailydata/kx20260826.dat": (200, SAMPLE_PAYLOAD),
    }
    with _Server(routes) as server:
        batch = make_adapter(server, search_days=3).fetch(cursor=None, now=NOW)

    assert batch.new_cursor == "20260826"
    assert batch.items[0].original_url.endswith("kx20260826.dat")
    assert batch.items[0].title.startswith("沪期货 镍：主力合约结算价")


def test_fetch_rolls_back_to_previous_trading_file() -> None:
    routes = {
        "/dailydata/kx20260826.dat": (404, None),
        "/dailydata/kx20260825.dat": (200, SAMPLE_PAYLOAD),
    }
    with _Server(routes) as server:
        batch = make_adapter(server, search_days=3).fetch(cursor="20260824", now=NOW)

    assert len(batch.items) == 2
    assert batch.new_cursor == "20260826"
    assert batch.items[0].original_url.endswith("kx20260825.dat")


def test_fetch_raises_source_fetch_error_when_all_days_missing() -> None:
    from intel_daily.adapters.shfe import ShfeAdapter

    with _Server({}) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server, search_days=2).fetch(cursor=None, now=NOW)


def test_fetch_raises_source_fetch_error_on_http_500() -> None:
    with _Server({"/dailydata/kx20260826.dat": (500, None)}) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server, search_days=2).fetch(cursor=None, now=NOW)


def test_fetch_raises_source_fetch_error_on_unreachable_server() -> None:
    from intel_daily.adapters.shfe import ShfeAdapter

    adapter = ShfeAdapter(
        data_base_url="http://127.0.0.1:1/dailydata/",
        search_days=2,
        user_agent="intel-daily/0.1",
    )
    with pytest.raises(SourceFetchError):
        adapter.fetch(cursor=None, now=NOW)
