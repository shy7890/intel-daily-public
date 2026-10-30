import json
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest


NOW = datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc)

SAMPLE_PAYLOAD = {
    "announcements": [
        {
            "secCode": "301358",
            "secName": "湖南裕能",
            "announcementId": "1225515073",
            "announcementTitle": "关于5%以上股东权益变动触及1%整数倍的公告",
            "announcementTime": 1787748010000,
            "adjunctUrl": "finalpage/2026-08-26/1225515073.PDF",
            "adjunctType": "PDF",
            "announcementType": "01010503||010112||010115||011501",
        },
        {
            "secCode": "301282",
            "secName": "金禄电子",
            "announcementId": "1225515037",
            "announcementTitle": "关于股东减持股份计划期限届满暨实施完毕的公告",
            "announcementTime": 1787748010000,
            "adjunctUrl": "finalpage/2026-08-26/1225515037.PDF",
            "adjunctType": "PDF",
            "announcementType": "01010503||010112||010115||011501",
        },
        {
            "secCode": "000066",
            "secName": "中国长城",
            "announcementId": "1234567890",
            "announcementTitle": "无 PDF 附件的公告",
            "announcementTime": 1787748010000,
            "adjunctUrl": None,
            "adjunctType": None,
        },
        {
            "secCode": "300750",
            "secName": "宁德时代",
            "announcementId": "1234567891",
            "announcementTitle": "无时间的公告",
            "announcementTime": 1787730000000,
            "adjunctUrl": None,
            "adjunctType": None,
        },
    ],
    "totalAnnouncement": 4,
    "hasMore": False,
}


def parse_payload(payload: dict):
    from intel_daily.adapters.cninfo import parse_cninfo_payload

    return parse_cninfo_payload(payload, fetched_at=NOW)


def test_parse_cninfo_payload_builds_items_with_pdf_links() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)

    assert len(items) == 2

    first = items[0]
    assert first.external_id == "1225515073"
    assert first.title == "关于5%以上股东权益变动触及1%整数倍的公告"
    assert first.publisher == "巨潮资讯网"
    assert first.published_at == datetime(2026, 8, 26, 20, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert (
        first.original_url
        == "https://static.cninfo.com.cn/finalpage/2026-08-26/1225515073.PDF"
    )
    assert first.keywords == ("湖南裕能",)
    assert first.summary == ""

    second = items[1]
    assert second.external_id == "1225515037"
    assert second.title == "关于股东减持股份计划期限届满暨实施完毕的公告"
    assert second.keywords == ("金禄电子",)


def test_parse_cninfo_payload_skips_announcements_without_link_or_time() -> None:
    items = parse_payload(SAMPLE_PAYLOAD)

    titles = [item.title for item in items]
    assert "无 PDF 附件的公告" not in titles
    assert "无时间的公告" not in titles


def test_parse_cninfo_payload_drops_invalid_rows() -> None:
    payload = {"announcements": [{"secCode": "000001", "secName": "平安银行"}]}
    assert parse_payload(payload) == []


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {}
    status: int = 200
    last_form: dict = {}

    def _respond(self) -> None:
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status)
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
        return f"http://127.0.0.1:{self.port}/query"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def make_adapter(server, columns=("szse",)):
    from intel_daily.adapters.cninfo import CninfoAdapter

    return CninfoAdapter(
        query_url=server.url,
        columns=columns,
        request_interval=0,
        user_agent="intel-daily/0.1",
    )


def test_cninfo_fetch_sends_daily_window_floor_and_ceil() -> None:
    with _Server(SAMPLE_PAYLOAD) as server:
        make_adapter(server).fetch(cursor="2026-08-25", now=NOW)

    form = _Handler.last_form
    assert form["pageNum"] == "1"
    assert form["column"] == "szse"
    assert form["tabName"] == "fulltext"
    assert form["seDate"] == "2026-08-25~2026-08-27"


def test_cninfo_fetch_returns_batch_and_advances_cursor() -> None:
    with _Server(SAMPLE_PAYLOAD) as server:
        items = make_adapter(server).fetch(cursor="2026-08-25", now=NOW)

    assert len(items.items) == 2
    assert items.new_cursor == "2026-08-26"
    assert [item.external_id for item in items.items] == [
        "1225515073",
        "1225515037",
    ]


def _announcement_row(
    ann_id: str, title: str, published_sh: datetime, pdf_date: str
) -> dict:
    return {
        "secCode": "300001",
        "secName": "特锐德",
        "announcementId": ann_id,
        "announcementTitle": title,
        "announcementTime": int(published_sh.timestamp() * 1000),
        "adjunctUrl": f"finalpage/{pdf_date}/{ann_id}.PDF",
        "adjunctType": "PDF",
        "announcementType": "01010503",
    }


def test_cninfo_fetch_drops_items_before_cursor_day() -> None:
    # 真“无新增”：cursor 晚于全部条目日期 → 无返回、cursor 保持
    payload = {"announcements": SAMPLE_PAYLOAD["announcements"]}
    with _Server(payload) as server:
        batch = make_adapter(server).fetch(
            cursor="2026-08-27",
            now=datetime(2026, 8, 28, 6, 0, 0, tzinfo=timezone.utc),
        )

    assert batch.items == []
    assert batch.new_cursor == "2026-08-27"


def test_cninfo_fetch_refetches_cursor_day_for_same_day_repeats() -> None:
    # cursor 停在已消费日时，同日再次运行必须重新扫描该日
    # （已入库条目靠 UNIQUE(source_id, external_id) 去重兜底），
    # 否则当日 09:00 后新公告永久丢失。
    with _Server(SAMPLE_PAYLOAD) as server:
        batch = make_adapter(server).fetch(cursor="2026-08-26", now=NOW)

    assert [item.external_id for item in batch.items] == [
        "1225515073",
        "1225515037",
    ]
    assert batch.new_cursor == "2026-08-26"


def test_cninfo_same_day_second_run_keeps_afternoon_announcements() -> None:
    # 回归：一天内多次运行不丢下午公告（旧实现 date > cursor 严格大于会丢）
    sh = ZoneInfo("Asia/Shanghai")
    morning = _announcement_row(
        "m1", "盘前公告", datetime(2026, 8, 27, 8, 30, tzinfo=sh), "2026-08-27"
    )
    afternoon = _announcement_row(
        "a1", "盘后公告", datetime(2026, 8, 27, 15, 30, tzinfo=sh), "2026-08-27"
    )
    with _Server({"announcements": [morning]}) as server:
        first = make_adapter(server).fetch(
            cursor=None,
            now=datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc),
        )
    assert [item.external_id for item in first.items] == ["m1"]
    assert first.new_cursor == "2026-08-27"

    with _Server({"announcements": [morning, afternoon]}) as server:
        second = make_adapter(server).fetch(
            cursor=first.new_cursor,
            now=datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
        )
    assert "a1" in [item.external_id for item in second.items]
    assert second.new_cursor == "2026-08-27"


def test_cninfo_fetch_first_run_uses_today_window() -> None:
    sh = ZoneInfo("Asia/Shanghai")
    today_payload = {
        "announcements": [
            _announcement_row(
                "t1", "当日公告", datetime(2026, 8, 27, 9, 0, tzinfo=sh), "2026-08-27"
            )
        ]
    }
    with _Server(today_payload) as server:
        batch = make_adapter(server).fetch(cursor=None, now=NOW)

    assert _Handler.last_form["seDate"] == "2026-08-27~2026-08-27"
    assert len(batch.items) == 1
    assert batch.new_cursor == "2026-08-27"


def test_cninfo_fetch_raises_on_non_200_and_unreachable() -> None:
    from intel_daily.adapters.nea import SourceFetchError
    from intel_daily.adapters.cninfo import CninfoAdapter

    with _Server(SAMPLE_PAYLOAD, status=500) as server:
        with pytest.raises(SourceFetchError):
            make_adapter(server).fetch(cursor=None, now=NOW)

    with pytest.raises(SourceFetchError):
        CninfoAdapter(
            query_url="http://127.0.0.1:1/query",
            request_interval=0,
            user_agent="intel-daily/0.1",
        ).fetch(cursor=None, now=NOW)
