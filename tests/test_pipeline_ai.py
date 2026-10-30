import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from intel_daily.adapters.nea import NeaAdapter
from intel_daily.db import init_database
from intel_daily.pipeline import run_source_once
from intel_daily.sources import Source
from intel_daily.summarizers import DeepSeekSummarizer


NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)
SH = ZoneInfo("Asia/Shanghai")

PAYLOAD = {
    "datasource": [
        {
            "contentId": "ai1",
            "title": "国家能源局关于印发新型储能试点工作方案的通知",
            "publishTime": "2026-08-21 17:37:04",
            "publishUrl": "../20260821/ai1/c.html",
            "summary": "新型储能试点工作方案摘要。",
            "keywords": "储能",
            "contentType": "News",
        }
    ]
}


def make_source(cursor: str | None, allow_external_ai: bool) -> Source:
    return Source(
        id=3,
        name="国家能源局",
        publisher="国家能源局",
        url="http://www.nea.gov.cn/",
        source_type="primary",
        priority_tier="P0",
        adapter_type="nea_json",
        topic="能源政策",
        region="中国大陆",
        enabled=True,
        license_risk="low",
        crawl_stability_risk="low-medium",
        last_success_cursor=cursor,
        last_success_at=None,
        allow_external_ai=allow_external_ai,
    )


class _Handler(BaseHTTPRequestHandler):
    calls: int = 0
    response_payload: dict = {"choices": [{"message": {"content": "模型生成的短摘要。"}}]}
    status: int = 200
    list_payload: dict = PAYLOAD

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(self.list_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        payload = (
            json.dumps(self.response_payload).encode("utf-8")
            if self.status == 200
            else b'{"error": "boom"}'
        )
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, status: int = 200) -> None:
        handler = type("BoundHandler", (_Handler,), {"status": status})
        self.handler_class = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def calls(self) -> int:
        return self.handler_class.calls

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _adapter(server: _Server) -> NeaAdapter:
    return NeaAdapter(
        list_url=f"{server.base_url}/list.json",
        base_url="http://www.nea.gov.cn/xwzx/nyyw.htm",
        user_agent="intel-daily/0.1",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "intel.db"
    init_database(path)
    return path


def test_disabled_source_never_calls_model(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    with _Server() as server:
        result = run_source_once(
            db_path,
            make_source(None, allow_external_ai=False),
            _adapter(server),
            NOW,
            summarizer=DeepSeekSummarizer(api_key="test-key", base_url=server.base_url),
        )
    assert server.calls == 0
    assert result.status == "success"
    row = ItemRepository(db_path).get_by_external_id(3, "ai1")
    assert row["summary"] == "新型储能试点工作方案摘要。"


def test_allowed_source_uses_model_summary(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    with _Server() as server:
        result = run_source_once(
            db_path,
            make_source(None, allow_external_ai=True),
            _adapter(server),
            NOW,
            summarizer=DeepSeekSummarizer(api_key="test-key", base_url=server.base_url),
        )
    assert server.calls == 1
    assert result.status == "success"
    row = ItemRepository(db_path).get_by_external_id(3, "ai1")
    assert row["summary"] == "模型生成的短摘要。"
    assert row["verification_status"] == "primary"


def test_model_failure_keeps_official_summary_and_marks_partial(db_path: Path) -> None:
    from intel_daily.items import ItemRepository
    from intel_daily.runs import RunRepository

    with _Server(status=500) as server:
        result = run_source_once(
            db_path,
            make_source(None, allow_external_ai=True),
            _adapter(server),
            NOW,
            summarizer=DeepSeekSummarizer(api_key="test-key", base_url=server.base_url),
        )
    assert server.calls == 1
    assert result.status == "partial"
    assert result.failed == 1
    assert "DeepSeek" in result.error
    row = ItemRepository(db_path).get_by_external_id(3, "ai1")
    assert row["summary"] == "新型储能试点工作方案摘要。"
    runs = RunRepository(db_path).list_recent()
    assert runs[0].status == "partial"
    assert runs[0].error_summary != ""


def test_gate_failure_marks_secondary_pending_and_skips_model(db_path: Path) -> None:
    from types import SimpleNamespace

    from intel_daily.adapters.contract import SourceItem
    from intel_daily.items import ItemRepository

    class CountingSummarizer:
        calls = 0

        def generate(self, text: str):
            type(self).calls += 1
            from intel_daily.summarizers import SummaryResult

            return SummaryResult(summary="不应出现", provider="deepseek")

    broken_item = SourceItem(
        external_id="broken1",
        title="残缺条目",
        publisher="",
        published_at=NOW,
        original_url="",
        summary="无信息来源。",
        keywords=(),
        fetched_at=NOW,
    )
    fake_client = SimpleNamespace(
        fetch=lambda **kwargs: SimpleNamespace(
            items=[broken_item], new_cursor="2026-08-21 17:37:04"
        )
    )
    summarizer = CountingSummarizer()

    result = run_source_once(
        db_path,
        make_source(None, allow_external_ai=True),
        fake_client,
        NOW,
        summarizer=summarizer,
    )

    assert summarizer.calls == 0
    assert result.status == "success"
    row = ItemRepository(db_path).get_by_external_id(3, "broken1")
    assert row["verification_status"] == "secondary_pending"


def test_stub_summarizer_keeps_official_summary_without_failure(
    db_path: Path,
) -> None:
    from intel_daily.items import ItemRepository
    from intel_daily.summarizers import StubSummarizer

    with _Server() as server:
        result = run_source_once(
            db_path,
            make_source(None, allow_external_ai=True),
            _adapter(server),
            NOW,
            summarizer=StubSummarizer(),
        )
    assert server.calls == 0
    assert result.status == "success"
    assert result.failed == 0
    row = ItemRepository(db_path).get_by_external_id(3, "ai1")
    assert row["summary"] == "新型储能试点工作方案摘要。"
