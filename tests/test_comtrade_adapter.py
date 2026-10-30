import json
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)
FETCHED_AT = datetime(2026, 8, 27, 7, 30, 0, tzinfo=timezone.utc)

EXPECTED_REAL_URL = (
    "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
    "?reporterCode=156&period=2024&cmdCode=850760&partnerCode=0&flowCode=X"
)

# 内嵌固定样本：按 2026-08-27 真实公开 API 响应形态复制（curl 实测）。
# 注意 preview 公开接口中 reporterDesc/cmdDesc/flowDesc 均为 null——
# 标题素材由模块内标签映射/代码回退提供，不得依赖描述字段。
SAMPLE_RECORD = {
    "typeCode": "C",
    "freqCode": "A",
    "refPeriodId": 20240101,
    "refYear": 2024,
    "refMonth": 52,
    "period": "2024",
    "reporterCode": 156,
    "reporterISO": None,
    "reporterDesc": None,
    "flowCode": "X",
    "flowDesc": None,
    "partnerCode": 0,
    "partnerISO": None,
    "partnerDesc": None,
    "partner2Code": 0,
    "partner2ISO": None,
    "partner2Desc": None,
    "classificationCode": "H6",
    "classificationSearchCode": "HS",
    "isOriginalClassification": True,
    "cmdCode": "850760",
    "cmdDesc": None,
    "aggrLevel": None,
    "isLeaf": None,
    "customsCode": "C00",
    "customsDesc": None,
    "mosCode": "0",
    "motCode": 0,
    "motDesc": None,
    "qtyUnitCode": 5,
    "qtyUnitAbbr": None,
    "qty": 3914411919.0,
    "isQtyEstimated": False,
    "altQtyUnitCode": 5,
    "altQtyUnitAbbr": None,
    "altQty": 3914411919.0,
    "isAltQtyEstimated": False,
    "netWgt": 3043361312.246,
    "isNetWgtEstimated": True,
    "grossWgt": 0.0,
    "isGrossWgtEstimated": False,
    "cifvalue": None,
    "fobvalue": 61126922874.0,
    "primaryValue": 61126922874.0,
    "legacyEstimationFlag": 4,
    "isReported": False,
    "isAggregate": True,
}


def record_with_period(period: str) -> dict:
    record = dict(SAMPLE_RECORD)
    if len(period) == 6:
        record["freqCode"] = "M"
        record["refPeriodId"] = int(period + "01")
        record["refYear"] = int(period[0:4])
        record["refMonth"] = int(period[4:6])
    record["period"] = period
    return record


def sample_payload(period: str, primary_value: float | None = 61126922874.0) -> dict:
    record = record_with_period(period)
    record = dict(record)
    record["primaryValue"] = primary_value
    return {"elapsedTime": "0.14 secs", "count": 1, "data": [record], "error": ""}


def parse_payload(payload: dict, requested_period: str = "2024"):
    from intel_daily.adapters.comtrade import parse_comtrade_payload

    return parse_comtrade_payload(payload, requested_period=requested_period, fetched_at=FETCHED_AT)


def test_parse_builds_item_from_annual_record() -> None:
    payload = {
        "count": 2,
        "data": [SAMPLE_RECORD, dict(SAMPLE_RECORD, primaryValue=None)],
    }
    items = parse_payload(payload)

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "850760-2024-X"
    assert item.title == "中国 2024 锂离子电池（HS 850760）出口额"
    assert item.publisher == "UN Comtrade"
    assert item.published_at == FETCHED_AT
    assert item.original_url == EXPECTED_REAL_URL
    assert item.summary.startswith("出口额 61,126,922,874 USD")
    assert item.keywords == ("锂离子电池",)
    assert item.fetched_at == FETCHED_AT


def test_parse_skips_row_without_primary_value() -> None:
    payload = {"data": [dict(SAMPLE_RECORD, primaryValue=None)]}
    assert parse_payload(payload) == []


def test_parse_tolerates_missing_fields_and_descriptions() -> None:
    payload = {
        "data": [
            {
                "typeCode": "C",
                "freqCode": "A",
                "period": "2024",
                "reporterCode": 156,
                "flowCode": "X",
                "partnerCode": 0,
                "classificationSearchCode": "HS",
                "cmdCode": "850760",
                "primaryValue": 1000.0,
            },
            {"primaryValue": "不是数字"},
            {"period": "2024"},
            {},
        ]
    }
    items = parse_payload(payload)

    assert len(items) == 1
    assert items[0].summary.startswith("出口额 1,000 USD")


def test_parse_empty_payload_returns_empty() -> None:
    assert parse_payload({"count": 0, "data": [], "error": ""}) == []
    assert parse_payload({}) == []


def test_parse_tolerates_non_numeric_reporter_code() -> None:
    """T6：reporterCode 可能是非数值（接口脏数据），不得抛 ValueError 中断整源。"""
    payload = {
        "data": [
            dict(
                SAMPLE_RECORD,
                reporterCode="CN",
                partnerCode="unknown",
                refYear="n/a",
            )
        ]
    }
    items = parse_payload(payload)

    assert len(items) == 1
    assert "156" in items[0].title or "中国" in items[0].title


def test_fetch_rejects_non_year_cursor_without_crashing() -> None:
    """T6：cursor 被写坏（非年度令牌）时抛 SourceFetchError，由上层记该源 failed。"""
    from intel_daily.adapters.nea import SourceFetchError

    with _Server() as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor="2024-01-01 00:00:00", now=NOW)
    assert server.request_count == 0


class _Handler(BaseHTTPRequestHandler):
    responses: dict = {}
    requests: list = []

    def do_GET(self) -> None:  # noqa: N802
        self.requests.append(self.path)
        params = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1]))
        period = params.get("period", "")
        status, payload = self.responses.get(period, (200, {"count": 0, "data": [], "error": ""}))
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(
        self, responses: dict[str, tuple[int, object]] | None = None
    ) -> None:
        self.responses = responses or {}

    def __enter__(self) -> "_Server":
        self.handler = type(
            "BoundHandler",
            (_Handler,),
            {"responses": self.responses, "requests": []},
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/preview/C/A/HS"

    @property
    def periods(self) -> list[str]:
        periods = []
        for path in self.handler.requests:
            params = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
            periods.append(params.get("period", ""))
        return periods

    @property
    def request_count(self) -> int:
        return len(self.handler.requests)


def make_adapter(server, window_years: int = 2):
    from intel_daily.adapters.comtrade import ComtradeAdapter

    return ComtradeAdapter(
        url_template=server.url
        + "?reporterCode={reporter_code}&period={period}&cmdCode={cmd_code}"
        "&partnerCode={partner_code}&flowCode={flow_code}",
        request_interval=0,
        user_agent="intel-daily/0.1",
        window_years=window_years,
    )


def test_fetch_requests_period_window_in_order() -> None:
    with _Server() as server:
        make_adapter(server).fetch(cursor=None, now=NOW)

    assert server.periods == ["2024", "2025"]
    assert server.request_count == 2


def test_fetch_returns_items_and_advances_cursor() -> None:
    responses = {
        "2024": (200, sample_payload("2024", 100.0)),
        "2025": (200, sample_payload("2025", 200.0)),
    }
    with _Server(responses) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert [item.external_id for item in batch.items] == [
        "850760-2024-X",
        "850760-2025-X",
    ]
    assert batch.new_cursor == "2025"
    first = batch.items[0]
    assert first.title == "中国 2024 锂离子电池（HS 850760）出口额"
    assert first.summary.startswith("出口额 100 USD")
    assert first.published_at == NOW


def test_fetch_skips_year_without_data_and_keeps_last_with_items() -> None:
    """2026-08-27 实测：2025 年度未发布（count=0）→ 跳过且 new_cursor 停在 2024。"""
    responses = {"2024": (200, sample_payload("2024", 100.0))}
    with _Server(responses) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert [item.external_id for item in batch.items] == ["850760-2024-X"]
    assert batch.new_cursor == "2024"


def test_fetch_keeps_cursor_when_no_new_period() -> None:
    with _Server() as server:
        batch = make_adapter(server).fetch(cursor="2025", now=NOW)

    assert batch.items == []
    assert batch.new_cursor == "2025"
    assert server.request_count == 0


def test_fetch_cursor_limits_window() -> None:
    responses = {"2025": (200, sample_payload("2025", 200.0))}
    with _Server(responses) as server:
        batch = make_adapter(server).fetch(cursor="2024", now=NOW)

    assert server.periods == ["2025"]
    assert [item.external_id for item in batch.items] == ["850760-2025-X"]
    assert batch.new_cursor == "2025"


def test_fetch_raises_on_429() -> None:
    from intel_daily.adapters.nea import SourceFetchError

    with _Server(responses={"2024": (429, {})}) as server:
        with pytest.raises(SourceFetchError) as excinfo:
            make_adapter(server).fetch(cursor=None, now=NOW)

    assert "429" in str(excinfo.value)


def test_fetch_raises_on_http_500() -> None:
    from intel_daily.adapters.nea import SourceFetchError

    with _Server(responses={"2024": (500, {})}) as server:
        with pytest.raises(SourceFetchError) as excinfo:
            make_adapter(server).fetch(cursor=None, now=NOW)

    assert "500" in str(excinfo.value)


def test_fetch_raises_on_unreachable_server() -> None:
    from intel_daily.adapters.comtrade import ComtradeAdapter
    from intel_daily.adapters.nea import SourceFetchError

    adapter = ComtradeAdapter(
        url_template="http://127.0.0.1:1/preview/C/A/HS?reporterCode={reporter_code}&period={period}&cmdCode={cmd_code}&partnerCode={partner_code}&flowCode={flow_code}",
        request_interval=0,
    )
    with pytest.raises(SourceFetchError):
        adapter.fetch(cursor=None, now=NOW)
