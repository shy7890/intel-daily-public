import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intel_daily.notifier import (
    FeishuWebhookNotifier,
    StubNotifier,
    render_digest,
)


def _row(**overrides) -> dict:
    values = dict(
        category="储能",
        importance="high",
        title="新型储能成为第一大调节性资源",
        summary="截至6月底全国新型储能装机达1.53亿千瓦，同比增长61%。",
        publisher="国家能源局",
        published_at="2026-08-21T09:37:04+00:00",
        original_url="http://www.nea.gov.cn/20260821/aaa/c.html",
        verification_status="primary",
    )
    values.update(overrides)
    return values


def test_render_digest_groups_by_category_and_carries_required_fields() -> None:
    rows = [
        _row(),
        _row(
            category="新能源",
            title="上半年全国光伏新增装机7207万千瓦",
            summary="光伏装机同比大幅增长。",
            importance="medium",
        ),
        _row(title="第二篇储能新闻", summary="储能继续增长。"),
    ]
    text = render_digest(rows, date_label="2026-08-26")

    assert "新能源每日情报 2026-08-26" in text
    assert "储能" in text
    assert "新能源" in text
    assert "新型储能成为第一大调节性资源" in text
    assert "上半年全国光伏新增装机7207万千瓦" in text
    assert "截至6月底全国新型储能装机达1.53亿千瓦，同比增长61%。" in text
    assert "国家能源局" in text
    assert "2026-08-21" in text
    assert "http://www.nea.gov.cn/20260821/aaa/c.html" in text
    assert "高" in text and "中" in text


def test_render_digest_flags_unverified_items() -> None:
    text = render_digest([_row(verification_status="secondary_pending")], "2026-08-26")
    assert "待核验" in text


def test_render_digest_drops_items_without_link() -> None:
    text = render_digest([_row(original_url="")], "2026-08-26")
    assert "新型储能成为第一大调节性资源" not in text
    assert "无有效原文链接的条目已跳过" in text or "已跳过" in text


class _Handler(BaseHTTPRequestHandler):
    received: dict = {}
    response: dict = {"code": 0, "msg": "success"}
    status: int = 200

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).received = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": json.loads(self.rfile.read(length) or b"{}"),
        }
        body = json.dumps(self.response).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, response: dict, status: int = 200) -> None:
        handler = type(
            "BoundHandler", (_Handler,), {"response": response, "status": status}
        )
        self.handler_class = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/open-apis/bot/v2/hook/test-token"

    @property
    def received(self) -> dict:
        return self.handler_class.received.copy()

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_webhook_notifier_sends_text_payload_and_succeeds() -> None:
    with _Server({"code": 0, "msg": "success"}) as server:
        receipt = FeishuWebhookNotifier(server.url).send("每日情报测试")

    assert receipt.ok is True
    assert receipt.error is None
    received = server.received
    assert received["path"].endswith("/open-apis/bot/v2/hook/test-token")
    assert received["content_type"].startswith("application/json")
    assert received["body"]["msg_type"] == "text"
    assert received["body"]["content"]["text"] == "每日情报测试"


def test_webhook_notifier_reports_business_error() -> None:
    with _Server({"code": 9499, "msg": "Bad Request"}) as server:
        receipt = FeishuWebhookNotifier(server.url).send("x")
    assert receipt.ok is False
    assert "9499" in receipt.error


def test_webhook_notifier_reports_http_errors() -> None:
    with _Server({}, status=500) as server:
        receipt = FeishuWebhookNotifier(server.url).send("x")
    assert receipt.ok is False


def test_stub_notifier_never_touches_network() -> None:
    receipt = StubNotifier().send("内容")
    assert receipt.ok is True
    assert receipt.dry_run is True


def test_build_digest_reads_latest_items(tmp_path) -> None:
    import sqlite3

    from intel_daily.db import init_database
    from intel_daily.notifier import build_digest

    db_path = tmp_path / "intel.db"
    init_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO intelligence_items (
                source_id, external_id, title, original_url, canonical_url,
                publisher, published_at, fetched_at, source_type,
                verification_status, category, importance, summary,
                content_fingerprint, status
            ) VALUES (3, 'digest-1', ?, ?, ?, ?, ?, ?, 'primary',
                      'primary', '储能', 'high', ?, 'sha256:digest', 'collected')
            """,
            (
                "来自数据库的条目",
                "http://www.nea.gov.cn/20260825/digest/c.html",
                "http://www.nea.gov.cn/20260825/digest/c.html",
                "国家能源局",
                "2026-08-25T10:00:00+00:00",
                "2026-08-26T00:00:00+00:00",
                "数据库摘要。",
            ),
        )

    text = build_digest(db_path, date_label="2026-08-26")
    assert "来自数据库的条目" in text
    assert "数据库摘要。" in text
    assert "储能" in text


def test_render_card_has_colored_header_and_markdown_body() -> None:
    from intel_daily.notifier import render_card

    card = render_card("🔥 【今日重点】\n1. ...", date_label="2026-08-26")
    assert card["msg_type"] == "interactive"
    header = card["card"]["header"]
    assert header["template"] == "blue"
    assert "今日新能源情报" in header["title"]["content"]
    assert card["card"]["elements"][0]["tag"] == "markdown"
    assert "今日重点" in card["card"]["elements"][0]["content"]


def test_webhook_notifier_sends_card_payload() -> None:
    from intel_daily.notifier import FeishuWebhookNotifier, render_card

    with _Server({"code": 0, "msg": "success"}) as server:
        receipt = FeishuWebhookNotifier(server.url).send_card(
            render_card("正文", date_label="2026-08-26")
        )
    assert receipt.ok is True
    body = server.received["body"]
    assert body["msg_type"] == "interactive"
    assert body["card"]["header"]["template"] == "blue"


def test_count_window_items_reports_total_beyond_fetch_limit(tmp_path) -> None:
    """T9 诊断口径：fetch_items 会被 LIMIT 截断，count_window_items 必须是窗口全量。"""
    import sqlite3
    from datetime import datetime, timezone

    from intel_daily.db import init_database
    from intel_daily.notifier import FETCH_LIMIT, count_window_items, fetch_items

    db_path = tmp_path / "intel.db"
    init_database(db_path)
    now = datetime.now(timezone.utc)
    total = FETCH_LIMIT + 3
    with sqlite3.connect(db_path) as connection:
        for i in range(total):
            connection.execute(
                """
                INSERT INTO intelligence_items (
                    source_id, external_id, title, original_url, canonical_url,
                    publisher, published_at, fetched_at, source_type,
                    verification_status, category, importance, summary,
                    content_fingerprint, status
                ) VALUES (3, ?, ?, ?, ?, '国家能源局', ?, ?, 'primary',
                          'primary', '储能', 'medium', '摘要', ?, 'collected')
                """,
                (
                    f"c{i}",
                    f"窗口计数条目{i}",
                    f"http://www.nea.gov.cn/c{i}",
                    f"http://www.nea.gov.cn/c{i}",
                    now.isoformat(),
                    now.isoformat(),
                    f"sha256:c{i}",
                ),
            )

    assert len(fetch_items(db_path, days=3)) == FETCH_LIMIT
    assert count_window_items(db_path, days=3) == total
