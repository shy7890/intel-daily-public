import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from intel_daily.adapters import SourceItem
from intel_daily.db import init_database
from intel_daily.sources import Source


NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)
SH = ZoneInfo("Asia/Shanghai")


def make_source(cursor: str | None, source_type: str = "primary") -> Source:
    return Source(
        id=3,
        name="国家能源局",
        publisher="国家能源局",
        url="http://www.nea.gov.cn/",
        source_type=source_type,
        priority_tier="P0",
        adapter_type="nea_json",
        topic="能源政策",
        region="中国大陆",
        enabled=True,
        license_risk="low",
        crawl_stability_risk="low-medium",
        last_success_cursor=cursor,
        last_success_at=None,
    )


def seed_item(external_id: str, title: str, when: datetime) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        title=title,
        publisher="国家能源局",
        published_at=when,
        original_url=f"http://www.nea.gov.cn/{external_id}/c.html",
        summary="已入库样本摘要",
        keywords=(),
        fetched_at=NOW,
    )


SAMPLE_PAYLOAD = {
    "datasource": [
        {
            "contentId": "new1",
            "title": "国家能源局关于印发新型储能试点工作方案的通知",
            "publishTime": "2026-08-21 17:37:04",
            "publishUrl": "../20260821/new1/c.html",
            "summary": "新型储能试点工作方案摘要。",
            "keywords": "储能,新型储能",
            "contentType": "News",
        },
        {
            "contentId": "new2",
            "title": "2026年上半年全国光伏新增装机7207万千瓦",
            "publishTime": "2026-08-22 09:00:00",
            "publishUrl": "../20260822/new2/c.html",
            "summary": "1.17亿千瓦可再生能源新增装机。",
            "keywords": "光伏",
            "contentType": "News",
        },
        {
            "contentId": "dupe1",
            "title": "已经入库过的标题",
            "publishTime": "2026-08-23 10:00:00",
            "publishUrl": "../20260823/dupe1/c.html",
            "summary": "重复样本。",
            "keywords": "",
            "contentType": "News",
        },
    ]
}


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
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/list.json"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "intel.db"
    init_database(path)
    return path


def test_run_source_once_ingests_classifies_and_dedupes(db_path: Path) -> None:
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.items import ItemRepository
    from intel_daily.pipeline import run_source_once

    repository = ItemRepository(db_path)
    repository.upsert(
        3,
        seed_item(
            "dupe1", "已经入库过的标题", datetime(2026, 8, 23, 10, 0, tzinfo=SH)
        ),
    )

    with _Server(SAMPLE_PAYLOAD) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        result = run_source_once(db_path, make_source(None), adapter, NOW)

    assert result.status == "success"
    assert result.collected == 2
    assert result.deduplicated == 1
    assert result.failed == 0
    assert result.new_cursor == "2026-08-23 10:00:00"

    rows = repository.list_recent(10)
    by_id = {row["external_id"]: row for row in rows}
    assert {"new1", "new2", "dupe1"} <= set(by_id)
    new1 = by_id["new1"]
    assert new1["original_url"] == "http://www.nea.gov.cn/20260821/new1/c.html"
    assert new1["published_at"].startswith("2026-08-21")
    assert new1["summary"] == "新型储能试点工作方案摘要。"
    assert new1["category"] == "储能"
    assert new1["importance"] == "high"
    new2 = by_id["new2"]
    assert new2["category"] == "新能源"
    assert new2["importance"] == "medium"


def test_run_source_once_is_idempotent_with_cursor(db_path: Path) -> None:
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.pipeline import run_source_once

    with _Server(SAMPLE_PAYLOAD) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        first = run_source_once(db_path, make_source(None), adapter, NOW)
        second = run_source_once(
            db_path, make_source(first.new_cursor), adapter, NOW
        )

    assert first.collected == 3
    assert second.collected == 0
    assert second.deduplicated == 0
    assert second.new_cursor == first.new_cursor
    assert second.status == "success"


def test_run_source_once_reports_failure(db_path: Path) -> None:
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.pipeline import run_source_once

    with _Server(SAMPLE_PAYLOAD, status=503) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        result = run_source_once(db_path, make_source(None), adapter, NOW)

    assert result.status == "failed"
    assert result.collected == 0
    assert result.new_cursor == ""


def test_run_source_once_records_run(db_path: Path) -> None:
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.pipeline import run_source_once
    from intel_daily.runs import RunRepository

    with _Server(SAMPLE_PAYLOAD) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        result = run_source_once(db_path, make_source(None), adapter, NOW)

    run = RunRepository(db_path).list_recent(1)[0]
    assert run.status == "success"
    assert run.kind == "国家能源局"
    assert run.collected_count == result.collected
    assert run.error_summary == ""


def test_run_source_once_records_failure_reason(db_path: Path) -> None:
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.pipeline import run_source_once
    from intel_daily.runs import RunRepository

    with _Server(SAMPLE_PAYLOAD, status=503) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        run_source_once(db_path, make_source(None), adapter, NOW)

    run = RunRepository(db_path).list_recent(1)[0]
    assert run.status == "failed"
    assert run.error_summary


def test_run_source_once_isolates_unexpected_errors(db_path: Path) -> None:
    """T6：非 SourceFetchError 的意外异常（脏数据/解析崩溃）也须记 failed 而非抛出。"""
    from intel_daily.pipeline import run_source_once
    from intel_daily.runs import RunRepository

    class BoomAdapter:
        def fetch(self, cursor, now):
            raise ValueError("接口返回非数值字段")

    result = run_source_once(db_path, make_source(None), BoomAdapter(), NOW)

    assert result.status == "failed"
    assert result.collected == 0
    assert "ValueError" in result.error
    run = RunRepository(db_path).list_recent(1)[0]
    assert run.status == "failed"
    assert "ValueError" in run.error_summary


def test_run_source_once_isolates_errors_during_batch_processing(db_path: Path) -> None:
    """T6：批处理途中崩溃（如坏条目）同样隔离，且不影响下一次运行。"""
    from intel_daily.adapters import FetchBatch, SourceItem
    from intel_daily.pipeline import run_source_once

    class HalfBrokenAdapter:
        def fetch(self, cursor, now):
            return FetchBatch(items=[None], new_cursor=cursor or "")  # type: ignore[list-item]

    result = run_source_once(db_path, make_source(None), HalfBrokenAdapter(), NOW)
    assert result.status == "failed"
    assert result.new_cursor == ""


def test_run_source_once_writes_started_at(db_path: Path) -> None:
    """T8：run 行开跑即写 started_at，运行记录页可显示耗时。"""
    from intel_daily.adapters.nea import NeaAdapter
    from intel_daily.pipeline import run_source_once
    from intel_daily.runs import RunRepository

    with _Server(SAMPLE_PAYLOAD) as server:
        adapter = NeaAdapter(
            list_url=server.url,
            base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
            user_agent="intel-daily/0.1",
        )
        run_source_once(db_path, make_source(None), adapter, NOW)

    run = RunRepository(db_path).list_recent(1)[0]
    assert run.started_at is not None
    assert run.started_at <= run.finished_at


def test_run_source_once_marks_secondary_source(db_path: Path) -> None:
    """T11：二手信源条目落库为 secondary/secondary_pending（不再硬编码 primary）。"""
    from intel_daily.adapters import FetchBatch, SourceItem
    from intel_daily.items import ItemRepository
    from intel_daily.pipeline import run_source_once

    class SingleItemAdapter:
        def fetch(self, cursor, now):
            return FetchBatch(
                items=[
                    SourceItem(
                        external_id="s1",
                        title="媒体报道的储能政策",
                        publisher="某媒体",
                        published_at=NOW,
                        original_url="https://media.example.com/s1",
                        summary="报道摘要。",
                        keywords=(),
                        fetched_at=NOW,
                    )
                ],
                new_cursor="2026-08-26 09:00:00",
            )

    source = Source(
        id=3,
        name="某媒体",
        publisher="某媒体",
        url="https://media.example.com/feed",
        source_type="secondary",
        priority_tier="P1",
        adapter_type="pending_html",
        topic="行业动态",
        region="中国大陆",
        enabled=True,
        license_risk="medium",
        crawl_stability_risk="medium",
        last_success_cursor=None,
        last_success_at=None,
    )
    result = run_source_once(db_path, source, SingleItemAdapter(), NOW)

    assert result.collected == 1
    row = ItemRepository(db_path).get_by_external_id(3, "s1")
    assert row["source_type"] == "secondary"
    assert row["verification_status"] == "secondary_pending"


def test_gate_rejects_non_http_url_but_keeps_item(db_path: Path) -> None:
    """T11：original_url 非 http/https 的条目保留在库中，但降级为 secondary_pending。"""
    from intel_daily.adapters import FetchBatch, SourceItem
    from intel_daily.items import ItemRepository
    from intel_daily.pipeline import run_source_once

    class OddLinkAdapter:
        def fetch(self, cursor, now):
            return FetchBatch(
                items=[
                    SourceItem(
                        external_id="ftp1",
                        title="文件链接公告",
                        publisher="国家能源局",
                        published_at=NOW,
                        original_url="ftp://files.example.gov.cn/a.pdf",
                        summary="摘要。",
                        keywords=(),
                        fetched_at=NOW,
                    )
                ],
                new_cursor="2026-08-26 09:00:00",
            )

    result = run_source_once(db_path, make_source(None), OddLinkAdapter(), NOW)

    assert result.collected == 1
    assert result.skipped == 1
    row = ItemRepository(db_path).get_by_external_id(3, "ftp1")
    assert row["verification_status"] == "secondary_pending"
    assert row["status"] in ("collected", "skipped")


def test_run_source_once_reingest_same_cursor_is_idempotent(db_path: Path) -> None:
    """T1 兜底：游标用 >= 后同日重跑会重新返回同一批条目，必须靠唯一约束幂等去重。"""
    from intel_daily.adapters import FetchBatch, SourceItem
    from intel_daily.items import ItemRepository
    from intel_daily.pipeline import run_source_once

    class SameDayAdapter:
        def __init__(self) -> None:
            self.batch_no = 0

        def fetch(self, cursor, now):
            self.batch_no += 1
            rows = [
                SourceItem(
                    external_id=f"a{i}",
                    title=f"公告事件{i}",
                    publisher="巨潮资讯网",
                    published_at=now,
                    original_url=f"http://static.cninfo.com.cn/a{i}.PDF",
                    summary="摘要。",
                    keywords=(),
                    fetched_at=now,
                )
                for i in range(3)
            ]
            if self.batch_no == 2:  # 下午新增一条，cursor 仍停在当天
                rows.append(
                    SourceItem(
                        external_id="a9",
                        title="盘后新增公告",
                        publisher="巨潮资讯网",
                        published_at=now,
                        original_url="http://static.cninfo.com.cn/a9.PDF",
                        summary="摘要。",
                        keywords=(),
                        fetched_at=now,
                    )
                )
            return FetchBatch(items=rows, new_cursor=cursor or "")

    adapter = SameDayAdapter()
    first = run_source_once(db_path, make_source("2026-08-26"), adapter, NOW)
    second = run_source_once(db_path, make_source("2026-08-26"), adapter, NOW)

    assert (first.collected, first.deduplicated) == (3, 0)
    # 同日第二次运行：已入库的 3 条被幂等去重，下午新增的 1 条正常入库
    assert (second.collected, second.deduplicated) == (1, 3)
    assert second.status == "success"
    assert ItemRepository(db_path).count() == 4
