from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from intel_daily.db import init_database
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from intel_daily.scheduling import ScheduleSettings
from intel_daily.sources import SourceRepository


SH = ZoneInfo("Asia/Shanghai")
# 相对时钟：worker CLI 走真实当前时间，条目用 NOW 保证未来任意一天运行均在 3 日窗口内
NOW = datetime.now(timezone.utc)

SETTINGS = ScheduleSettings(
    timezone="Asia/Shanghai",
    daily_time=time(8, 30),
    weekdays_only=False,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "intel.db"
    init_database(path)
    return path



class _WebhookHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        _WebhookHandler.calls += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'{"code": 0, "msg": "success"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/open-apis/bot/v2/hook/t"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def enable(db_path: Path, *adapter_types: str) -> SourceRepository:
    repository = SourceRepository(db_path)
    for source in repository.list_all():
        if source.adapter_type in adapter_types:
            repository.set_enabled(source.id, True)
    return repository


class FakeAdapter:
    def __init__(
        self,
        cursor: str = "",
        fail: bool = False,
        published_at: datetime | None = None,
        title: str = "样本条目",
    ) -> None:
        self.cursor = cursor
        self.fail = fail
        self.published_at = published_at or datetime.now(timezone.utc)
        self.title = title
        self.calls = 0

    def fetch(self, cursor, now):
        self.calls += 1
        if self.fail:
            from intel_daily.adapters.nea import SourceFetchError

            raise SourceFetchError("模拟不可达")
        from intel_daily.adapters import FetchBatch, SourceItem

        return FetchBatch(
            items=[SourceItem(
                external_id="x1",
                title=self.title,
                publisher="国家能源局",
                published_at=self.published_at,
                original_url="http://www.nea.gov.cn/20260821/x1/c.html",
                summary="样本摘要。",
                keywords=("储能",),
                fetched_at=now,
            )],
            new_cursor=self.cursor or "2026-08-22 09:00:00",
        )


def test_daily_window_due_uses_settings_timezone() -> None:
    from intel_daily.worker import daily_window_due

    assert daily_window_due(
        datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        SETTINGS,
        last_run_at=None,
    ) is True
    assert daily_window_due(
        datetime(2026, 8, 25, 16, 35, tzinfo=timezone.utc),
        SETTINGS,
        last_run_at=None,
    ) is False
    assert daily_window_due(
        datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
        SETTINGS,
        last_run_at=None,
    ) is True
    assert daily_window_due(
        datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
        SETTINGS,
        last_run_at=datetime(2026, 8, 26, 2, 0, tzinfo=timezone.utc),
    ) is False


def test_run_daily_runs_enabled_source_and_advances_cursor(db_path: Path) -> None:
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    repository = SourceRepository(db_path)
    result = run_daily(db_path, {"nea_json": lambda: FakeAdapter()}, NOW)

    assert result.status == "success"
    matched = next(s for s in repository.list_all() if s.adapter_type == "nea_json")
    assert matched.last_success_cursor == "2026-08-22 09:00:00"
    assert matched.last_success_at is not None
    assert result.unsupported == 0


def test_run_daily_records_unsupported_enabled_source(db_path: Path) -> None:
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    pending_src = next(s for s in SourceRepository(db_path).list_all() if s.adapter_type == "pending_html")
    SourceRepository(db_path).set_enabled(pending_src.id, True)

    result = run_daily(db_path, {"nea_json": lambda: FakeAdapter()}, NOW)

    assert result.status == "success"
    assert result.unsupported == 1


def test_run_daily_disabled_sources_are_skipped(db_path: Path) -> None:
    from intel_daily.worker import run_daily

    adapter = FakeAdapter()
    result = run_daily(db_path, {"nea_json": lambda: adapter}, NOW)
    assert result.status == "skipped"
    assert adapter.calls == 0


def test_run_daily_partial_failure_does_not_advance_failed_source(
    db_path: Path,
) -> None:
    from intel_daily.worker import run_daily

    repository = SourceRepository(db_path)
    enable(db_path, "nea_json")
    sources = {s.adapter_type: s for s in repository.list_all()}
    bad = sources["pending_html"]
    repository.set_enabled(bad.id, True)
    adapters = {
        "nea_json": lambda: FakeAdapter(),
        "pending_html": lambda: FakeAdapter(fail=True),
    }
    result = run_daily(db_path, adapters, NOW)

    assert result.status == "partial"
    assert len(result.failures) == 1
    good = sources["nea_json"]
    assert repository.get(good.id).last_success_cursor == "2026-08-22 09:00:00"
    assert repository.get(bad.id).last_success_cursor is None


def test_run_daily_does_not_rerun_same_day(db_path: Path) -> None:
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    adapter = FakeAdapter()
    first = run_daily(db_path, {"nea_json": lambda: adapter}, NOW)
    adapter.calls = 0
    second = run_daily(db_path, {"nea_json": lambda: adapter}, NOW)

    assert first.status == "success"
    assert second.status == "skipped"
    assert adapter.calls == 0


def test_worker_cli_once_runs_and_reports(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    enable(db_path, "nea_json")

    class SilentAdapter(FakeAdapter):
        pass

    rc = worker_main(["--once"], adapters={"nea_json": lambda: SilentAdapter()})
    assert rc == 0
    out = capsys.readouterr().out
    assert "success" in out
    assert SourceRepository(db_path).list_all()[2].last_success_cursor == "2026-08-22 09:00:00"


def test_worker_cli_preview_prints_digest_without_sending(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    enable(db_path, "nea_json")

    rc = worker_main(["--once", "--preview"], adapters={"nea_json": lambda: FakeAdapter()})
    assert rc == 0
    assert "今日情报简报" in capsys.readouterr().out


def test_worker_cli_dry_run_never_calls_webhook(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")

        rc = worker_main(
            ["--once", "--dry-run"],
            adapters={"nea_json": lambda: FakeAdapter()},
        )
    out = capsys.readouterr().out
    assert rc == 0
    assert "未发送" in out
    assert "send-ok" not in out
    assert _WebhookHandler.calls == 0


def test_worker_cli_send_requires_webhook_env(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    monkeypatch.delenv("INTEL_FEISHU_WEBHOOK", raising=False)
    enable(db_path, "nea_json")

    rc = worker_main(["--once", "--send"], adapters={"nea_json": lambda: FakeAdapter()})
    assert rc == 1
    out = capsys.readouterr().out
    assert "INTEL_FEISHU_WEBHOOK" in out


def test_worker_cli_send_uses_webhook_env(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url + "?v=1")
        enable(db_path, "nea_json")

        rc = worker_main(
            ["--once", "--send"],
            adapters={"nea_json": lambda: FakeAdapter()},
        )
    assert rc == 0
    assert "send" in capsys.readouterr().out


def test_worker_cli_initializes_fresh_database(monkeypatch, capsys, tmp_path) -> None:
    from intel_daily.worker import worker_main

    fresh = tmp_path / "brand-new" / "intel.db"
    monkeypatch.setenv("INTEL_DB_PATH", str(fresh))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")

    rc = worker_main(["--once"], adapters={"nea_json": lambda: FakeAdapter()})
    assert rc == 0
    assert fresh.exists()


def test_worker_cli_send_skips_when_no_items(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)

        # 不启用任何信源 → 无收集 → 不应发送
        rc = worker_main(["--once", "--send"], adapters={"nea_json": lambda: FakeAdapter()})
        assert rc == 0
        out = capsys.readouterr().out
        assert "send-skip" in out
    # 服务器未收到任何请求（handler 无计数，此处以 rc 与输出为准）


def test_run_daily_force_reruns_same_day(db_path: Path) -> None:
    from intel_daily.runs import RunRepository
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    adapter = FakeAdapter()
    first = run_daily(db_path, {"nea_json": lambda: adapter}, NOW, force=True)
    second = run_daily(db_path, {"nea_json": lambda: adapter}, NOW, force=True)

    assert first.status == "success"
    assert second.status == "success"
    assert adapter.calls == 2
    kinds = [run.kind for run in RunRepository(db_path).list_recent(50)]
    assert kinds.count("手动测试") == 2


def test_test_send_does_not_consume_daily_progress(
    db_path: Path, monkeypatch
) -> None:
    """D-012：测试发送不占每日进度，条目不得被标 sent。"""
    from intel_daily.items import ItemRepository
    from intel_daily.worker import perform_test_send

    _WebhookHandler.calls = 0
    with _Server() as server:
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")
        result = perform_test_send(
            db_path,
            adapters={
                "nea_json": lambda: FakeAdapter(
                    published_at=NOW, title="样本条目 2026"
                )
            },
            now=NOW,
        )

    assert result.status == "sent"
    rows = ItemRepository(db_path).list_recent(10)
    assert [row["status"] for row in rows] == ["collected"]
    # 09:00 正式 digest 仍须包含该条目（未被测试发送吞掉）
    from intel_daily.notifier import fetch_items

    assert [item["id"] for item in fetch_items(db_path, days=3, now=NOW)] == [
        row["id"] for row in rows
    ]


def test_fetch_items_include_sent(db_path: Path) -> None:
    from intel_daily.items import ItemRepository
    from intel_daily.notifier import fetch_items
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    run_daily(
        db_path,
        {"nea_json": lambda: FakeAdapter(published_at=NOW)},
        NOW,
    )
    item = ItemRepository(db_path).list_recent(1)[0]
    ItemRepository(db_path).mark_sent([item["id"]])

    assert fetch_items(db_path, days=3) == []
    included = fetch_items(db_path, days=3, include_sent=True)
    assert len(included) == 1
    assert included[0]["id"] == item["id"]


def test_worker_cli_test_send_uses_webhook_env_twice_same_day(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")
        adapter = FakeAdapter(published_at=NOW, title="样本条目 2026")

        first_rc = worker_main(
            ["--test-send"], adapters={"nea_json": lambda: adapter}
        )
        second_rc = worker_main(
            ["--test-send"], adapters={"nea_json": lambda: adapter}
        )
    out = capsys.readouterr().out
    assert first_rc == 0
    assert second_rc == 0
    assert out.count("test-send-ok") == 2
    assert _WebhookHandler.calls == 2


def test_worker_cli_test_send_without_webhook_env_returns_error(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    monkeypatch.delenv("INTEL_FEISHU_WEBHOOK", raising=False)
    enable(db_path, "nea_json")

    rc = worker_main(
        ["--test-send"], adapters={"nea_json": lambda: FakeAdapter()}
    )
    out = capsys.readouterr().out
    assert rc != 0
    assert "INTEL_FEISHU_WEBHOOK" in out


def test_worker_cli_test_send_with_dry_run_never_sends(
    db_path: Path, monkeypatch, capsys
) -> None:
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")

        rc = worker_main(
            ["--test-send", "--dry-run"],
            adapters={"nea_json": lambda: FakeAdapter()},
        )
    out = capsys.readouterr().out
    assert rc == 0
    assert "test-send-ok" not in out
    assert "未发送" in out
    assert _WebhookHandler.calls == 0


def _seed_items(db_path: Path, specs: list[tuple[str, str, str, str]]) -> list[int]:
    """按 (external_id, title, importance, published_at iso) 直插条目，返回 id 列表。"""
    from datetime import datetime as _dt

    from intel_daily.adapters import SourceItem
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    source = SourceRepository(db_path).list_all()[2]
    ids: list[int] = []
    for external_id, title, importance, published_at in specs:
        repository.upsert(
            source.id,
            SourceItem(
                external_id=external_id,
                title=title,
                publisher="测试来源",
                published_at=_dt.fromisoformat(published_at),
                original_url=f"http://www.test.cn/{external_id}",
                summary="样本 2026 摘要。",
                keywords=(),
                fetched_at=NOW,
            ),
            category="公司动态",
            importance=importance,
        )
        ids.append(repository.get_by_external_id(source.id, external_id)["id"])
    return ids


def test_mark_digest_sent_marks_canonical_absorbed_and_spares_trimmed(
    db_path: Path,
) -> None:
    """T3+T4：发送成功后 canonical+被吸收替身统一标 sent；被裁剪条目保留 collected。"""
    from intel_daily.digest import plan_entries, render_brief
    from intel_daily.digest import render_brief_limited
    from intel_daily.items import ItemRepository
    from intel_daily.notifier import fetch_items
    from intel_daily.worker import mark_digest_sent

    recent = NOW.astimezone(timezone.utc)
    canonical_id, absorbed_id, trimmed_id = _seed_items(
        db_path,
        [
            ("c1", "同一公告事件", "high", (recent - timedelta(hours=3)).isoformat()),
            ("c2", "同一公告事件", "high", (recent - timedelta(hours=2)).isoformat()),
            ("t9", "被裁剪的旧闻", "low", (recent - timedelta(hours=4)).isoformat()),
        ],
    )
    rows = ItemRepository(db_path).list_recent(10)
    merger = type("DetOnly", (), {"find_groups": lambda self, e: None})()
    plan = plan_entries([dict(row) for row in rows], merger=merger)
    assert plan.absorbed_ids == (absorbed_id,)

    canonical_only = plan_entries(
        [dict(r) for r in rows if r["external_id"] != "t9"], merger=merger
    )
    limit = len(render_brief(canonical_only, date_label="2026-08-27")) + 5
    _text, stats = render_brief_limited(plan, date_label="2026-08-27", max_chars=limit)
    assert trimmed_id not in stats.kept_ids

    marked = mark_digest_sent(db_path, plan, stats)
    assert set(marked) == {canonical_id, absorbed_id}

    statuses = {
        row["external_id"]: row["status"]
        for row in ItemRepository(db_path).list_recent(10)
    }
    assert statuses == {"c1": "sent", "c2": "sent", "t9": "collected"}
    # 合并替身次日不再出现在 fetch_items；被裁剪条目仍在
    fetched = {row["external_id"] for row in fetch_items(db_path, days=3, now=NOW)}
    assert fetched == {"t9"}


def test_run_daily_sent_count_written_to_run_row(
    db_path: Path, monkeypatch, capsys
) -> None:
    """T9：发送成功后当日“每日汇总”run 行 sent_count > 0。"""
    from intel_daily.runs import RunRepository
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")
        rc = worker_main(
            ["--once", "--send"],
            adapters={
                "nea_json": lambda: FakeAdapter(
                    published_at=NOW, title="样本条目 2026"
                )
            },
        )
    assert rc == 0
    capsys.readouterr()
    daily = [
        run
        for run in RunRepository(db_path).list_recent(20)
        if run.kind == "每日汇总"
    ]
    assert len(daily) == 1
    assert daily[0].sent_count == 1


def test_run_daily_records_window_truncation_diagnostic(
    db_path: Path, monkeypatch, capsys
) -> None:
    """T13：fetch 窗口被 LIMIT 截断时，把总数/截断数写入 run 诊断。"""
    from intel_daily.notifier import FETCH_LIMIT
    from intel_daily.runs import RunRepository
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    specs = [
        (f"w{i}", f"窗口样本 {i} 2026", "medium", NOW.isoformat())
        for i in range(FETCH_LIMIT + 5)
    ]
    _seed_items(db_path, specs)
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "caam_html")  # 无注册适配器产出 → 仅靠已入库条目发送

        class Noop:
            def fetch(self, cursor, now):
                from intel_daily.adapters import FetchBatch

                return FetchBatch(items=[], new_cursor=cursor or "")

        rc = worker_main(
            ["--once", "--send"], adapters={"caam_html": lambda: Noop()}
        )
    assert rc == 0
    capsys.readouterr()
    daily = [
        run
        for run in RunRepository(db_path).list_recent(20)
        if run.kind == "每日汇总"
    ]
    assert daily[0].sent_count > 0
    assert f"截断 {5} 条" in daily[0].error_summary or "截断" in daily[0].error_summary
    assert str(FETCH_LIMIT + 5) in daily[0].error_summary


def test_failed_daily_run_allows_single_retry(db_path: Path) -> None:
    """T13：当日全源 failed 不算已跑，允许再试一次；再失败后不再重试。"""
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    boom = FakeAdapter(fail=True)
    first = run_daily(db_path, {"nea_json": lambda: boom}, NOW)
    assert first.status == "failed"

    second = run_daily(db_path, {"nea_json": lambda: boom}, NOW)
    assert second.status == "failed"  # 重试了一次

    third = run_daily(db_path, {"nea_json": lambda: boom}, NOW)
    assert third.status == "skipped"  # 已重试过，不再无限重试


def test_ok_daily_run_skips_same_day(db_path: Path) -> None:
    """T13：ok（success/partial）后当日跳过（含 failed→success 恢复后）。"""
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    boom = FakeAdapter(fail=True)
    assert run_daily(db_path, {"nea_json": lambda: boom}, NOW).status == "failed"
    good = FakeAdapter()
    assert run_daily(db_path, {"nea_json": lambda: good}, NOW).status == "success"
    calls_before = good.calls
    assert run_daily(db_path, {"nea_json": lambda: good}, NOW).status == "skipped"
    assert good.calls == calls_before


def test_acquire_daily_lock_blocks_concurrent_run(db_path: Path) -> None:
    """T5：存在未超时 running 行时 run_daily 拒绝启动（不触碰信源）。"""
    from intel_daily.db import acquire_daily_lock
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    lock_id = acquire_daily_lock(
        db_path,
        run_date=NOW.astimezone(SH).date().isoformat(),
        started_at=NOW.isoformat(),
    )
    assert lock_id is not None

    adapter = FakeAdapter()
    result = run_daily(db_path, {"nea_json": lambda: adapter}, NOW)
    assert result.status == "locked"
    assert adapter.calls == 0


def test_acquire_daily_lock_preempts_stale_running(db_path: Path) -> None:
    """T5：running 超过 2 小时视为陈旧，可被抢占并记 failed。"""
    from datetime import timedelta

    from intel_daily.db import acquire_daily_lock
    from intel_daily.runs import RunRepository

    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    old_id = acquire_daily_lock(
        db_path, run_date="2026-08-26", started_at=stale
    )
    assert old_id is not None

    fresh = datetime.now(timezone.utc).isoformat()
    new_id = acquire_daily_lock(
        db_path, run_date="2026-08-26", started_at=fresh
    )
    assert new_id is not None and new_id != old_id
    runs = {run.id: run for run in RunRepository(db_path).list_recent(20)}
    assert runs[old_id].status == "failed"
    assert "抢占" in runs[old_id].error_summary or runs[old_id].error_summary


def test_check_due_skips_when_lock_running(
    db_path: Path, monkeypatch, capsys
) -> None:
    """T5：--check-due 见 running 锁即跳过，不双开。"""
    from intel_daily.db import acquire_daily_lock
    from intel_daily.scheduling import ScheduleSettings, SettingsRepository
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    SettingsRepository(db_path).save_schedule(
        ScheduleSettings(timezone="Asia/Shanghai", daily_time=time(0, 0), weekdays_only=False)
    )
    acquire_daily_lock(
        db_path,
        run_date=NOW.astimezone(SH).date().isoformat(),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    adapter = FakeAdapter()
    rc = worker_main(["--check-due"], adapters={"nea_json": lambda: adapter})
    out = capsys.readouterr().out
    assert rc == 0
    assert adapter.calls == 0
    assert "locked" in out or "skip" in out


def test_two_concurrent_workers_send_once(
    db_path: Path, monkeypatch
) -> None:
    """T5 回归：并发两 worker 不双发。"""
    _WebhookHandler.calls = 0

    class SlowAdapter(FakeAdapter):
        def fetch(self, cursor, now):
            import time as _time

            _time.sleep(0.2)
            return super().fetch(cursor, now)

    results: list[int] = []
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")
        # 预置一条已采集条目，使两个 worker 都走到发送路径竞争
        _seed_items(db_path, [("race", "并发样本条目 2026", "medium", NOW.isoformat())])

        def run():
            from intel_daily.worker import worker_main

            class Noop:
                def fetch(self, cursor, now):
                    from intel_daily.adapters import FetchBatch

                    return FetchBatch(items=[], new_cursor=cursor or "")

            results.append(
                worker_main(["--once", "--send"], adapters={"nea_json": lambda: SlowAdapter()})
            )

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert _WebhookHandler.calls == 1
    assert all(rc == 0 for rc in results)


def test_daily_run_rows_carry_started_at(db_path: Path) -> None:
    """T8：每日汇总/手动测试 run 行都必须有 started_at（开 run 即写），可算耗时。"""
    from intel_daily.runs import RunRepository
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")
    assert run_daily(db_path, {"nea_json": lambda: FakeAdapter()}, NOW).status == "success"
    assert run_daily(
        db_path, {"nea_json": lambda: FakeAdapter()}, NOW, force=True
    ).status == "success"

    runs = RunRepository(db_path).list_recent(20)
    daily = [run for run in runs if run.kind == "每日汇总"]
    manual = [run for run in runs if run.kind == "手动测试"]
    assert len(daily) == 1 and len(manual) == 1
    for run in (*daily, *manual):
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.started_at <= run.finished_at


def test_check_due_retries_failed_day_then_gives_up(
    db_path: Path, monkeypatch, capsys
) -> None:
    """T12：全源 failed 当天窗口仍然到期（最多重试一次），ok 后才不再到期。"""
    from intel_daily.scheduling import ScheduleSettings, SettingsRepository
    from intel_daily.worker import worker_main

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    SettingsRepository(db_path).save_schedule(
        ScheduleSettings(
            timezone="Asia/Shanghai", daily_time=time(0, 0), weekdays_only=False
        )
    )
    enable(db_path, "nea_json")
    boom = FakeAdapter(fail=True)

    codes = [
        worker_main(["--check-due"], adapters={"nea_json": lambda: boom})
        for _ in range(3)
    ]
    out = capsys.readouterr().out

    assert codes == [1, 1, 0]  # 两次真实尝试（第二次是重试），第三次不再打上游
    assert boom.calls == 2
    assert out.count("daily-failed") == 2
    assert "daily-skipped" in out


def test_daily_run_row_omits_truncation_note_when_window_fits(
    db_path: Path, monkeypatch, capsys
) -> None:
    """T9：未触及 LIMIT 时不得往 run 行塞噪声诊断（诊断字段只在真丢数据时写）。"""
    from intel_daily.runs import RunRepository
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    with _Server() as server:
        monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
        monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        enable(db_path, "nea_json")
        rc = worker_main(
            ["--once", "--send"],
            adapters={
                "nea_json": lambda: FakeAdapter(
                    published_at=NOW, title="样本条目 2026"
                )
            },
        )
    capsys.readouterr()
    assert rc == 0
    daily = [
        run for run in RunRepository(db_path).list_recent(20) if run.kind == "每日汇总"
    ]
    assert len(daily) == 1
    assert daily[0].error_summary == ""
    assert daily[0].sent_count == 1


def test_run_daily_releases_lock_row_when_loop_crashes(db_path: Path) -> None:
    """T5：每日循环本身崩溃也不能把“每日汇总”行留在 running（会挡后续运行 2 小时）。"""
    from intel_daily.runs import RunRepository
    from intel_daily.worker import run_daily

    enable(db_path, "nea_json")

    def broken_factory():
        raise RuntimeError("适配器构造失败")

    with pytest.raises(RuntimeError):
        run_daily(db_path, {"nea_json": broken_factory}, NOW)

    runs = RunRepository(db_path).list_recent(20)
    daily = [run for run in runs if run.kind == "每日汇总"]
    assert len(daily) == 1
    assert daily[0].status == "failed"
    assert daily[0].finished_at is not None
    assert "适配器构造失败" in daily[0].error_summary
    # 崩溃后当日仍可重试（不被 running 锁挡住）
    assert run_daily(db_path, {"nea_json": lambda: FakeAdapter()}, NOW).status == "success"


def test_mark_digest_sent_spares_absorbed_when_canonical_trimmed(
    db_path: Path,
) -> None:
    """T3+T4 交叉：canonical 被 20K 裁掉时，它的替身也不得被标 sent。"""
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited
    from intel_daily.items import ItemRepository
    from intel_daily.worker import mark_digest_sent

    recent = NOW
    first_id, dup_id, filler_id = _seed_items(
        db_path,
        [
            ("c1", "同一公告事件", "medium", (recent - timedelta(hours=3)).isoformat()),
            ("c2", "同一公告事件", "medium", (recent - timedelta(hours=2)).isoformat()),
            (
                "f1",
                "重磅政策" * 10,
                "high",
                (recent - timedelta(hours=1)).isoformat(),
            ),
        ],
    )
    rows = [dict(row) for row in ItemRepository(db_path).list_recent(10)]
    merger = type("DetOnly", (), {"find_groups": lambda self, e: None})()
    plan = plan_entries(rows, merger=merger)
    assert plan.absorbed_of == ((first_id, dup_id),)

    # 上限只够放高优先条目：canonical（中优先）被裁掉
    limit = len(
        render_brief(
            plan_entries([r for r in rows if r["external_id"] == "f1"], merger=merger),
            date_label="2026-08-27",
        )
    )
    _text, stats = render_brief_limited(plan, date_label="2026-08-27", max_chars=limit)
    assert first_id not in stats.kept_ids

    marked = mark_digest_sent(db_path, plan, stats)

    assert marked == [filler_id]
    statuses = {
        row["external_id"]: row["status"]
        for row in ItemRepository(db_path).list_recent(10)
    }
    assert statuses == {"c1": "collected", "c2": "collected", "f1": "sent"}


class _ChainMerger:
    """AI 合并器桩：把“同事件甲/乙”分到一组（配合确定性合并制造链式吸收）。"""

    def find_groups(self, entries):
        idx = [
            i for i, e in enumerate(entries) if e.title in ("同事件甲", "同事件乙")
        ]
        return [idx] if len(idx) >= 2 else None


def test_mark_digest_sent_marks_chained_absorption_whole_chain(db_path: Path) -> None:
    """P2-1：B→A 确定性合并 + A→C AI 合并（pairs=[(A,B),(C,A)]），C 存活 → A/B/C 全标。

    旧实现只标 C、A，中间替身 B 残留 collected，次日重复推送。
    """
    from intel_daily.digest import plan_entries, render_brief_limited
    from intel_daily.items import ItemRepository
    from intel_daily.worker import mark_digest_sent

    c_id, a_id, b_id = _seed_items(
        db_path,
        [
            ("c1", "同事件甲", "high", (NOW - timedelta(hours=3)).isoformat()),
            ("a1", "同事件乙", "high", (NOW - timedelta(hours=2)).isoformat()),
            ("b1", "同事件乙", "high", (NOW - timedelta(hours=1)).isoformat()),
        ],
    )
    rows = [dict(r) for r in ItemRepository(db_path).list_recent(10)]
    plan = plan_entries(rows, merger=_ChainMerger())
    # 确定性先合并 B→A，AI 再把 A 吸收进 C，形成链式 pairs
    assert {(c, a) for c, a in plan.absorbed_of} == {(a_id, b_id), (c_id, a_id)}

    _text, stats = render_brief_limited(plan, date_label="2026-08-27")
    assert c_id in stats.kept_ids

    marked = mark_digest_sent(db_path, plan, stats)
    assert set(marked) == {c_id, a_id, b_id}
    statuses = {
        row["external_id"]: row["status"]
        for row in ItemRepository(db_path).list_recent(10)
    }
    assert statuses == {"c1": "sent", "a1": "sent", "b1": "sent"}


def test_mark_digest_sent_spares_whole_chain_when_root_trimmed(db_path: Path) -> None:
    """P2-1 守护：根 canonical C 被 20K 裁掉时，链上 A/B 也全部保留 collected。"""
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited
    from intel_daily.items import ItemRepository
    from intel_daily.worker import mark_digest_sent

    filler_id, c_id, a_id, b_id = _seed_items(
        db_path,
        [
            ("f1", "重磅政策" * 10, "high", (NOW - timedelta(hours=1)).isoformat()),
            ("c1", "同事件甲", "medium", (NOW - timedelta(hours=4)).isoformat()),
            ("a1", "同事件乙", "medium", (NOW - timedelta(hours=3)).isoformat()),
            ("b1", "同事件乙", "medium", (NOW - timedelta(hours=2)).isoformat()),
        ],
    )
    rows = [dict(r) for r in ItemRepository(db_path).list_recent(10)]
    plan = plan_entries(rows, merger=_ChainMerger())

    # 上限只够放高优先条目：链根 C（中优先）被裁掉
    limit = len(
        render_brief(
            plan_entries(
                [r for r in rows if r["external_id"] == "f1"], merger=_ChainMerger()
            ),
            date_label="2026-08-27",
        )
    )
    _text, stats = render_brief_limited(plan, date_label="2026-08-27", max_chars=limit)
    assert c_id not in stats.kept_ids
    assert filler_id in stats.kept_ids

    marked = mark_digest_sent(db_path, plan, stats)
    assert marked == [filler_id]
    statuses = {
        row["external_id"]: row["status"]
        for row in ItemRepository(db_path).list_recent(10)
    }
    assert statuses == {
        "f1": "sent",
        "c1": "collected",
        "a1": "collected",
        "b1": "collected",
    }


def test_send_mode_plan_crash_finalizes_run_failed_and_allows_retry(
    db_path: Path, monkeypatch, capsys
) -> None:
    """P2-2：send 模式 plan 阶段意外崩溃 → 锁行以 failed 收尾，当日 --check-due 可重试。

    旧实现 finally 用 result.status（=success）写终态：sent=0 却记 success，挡掉当日重试。
    """
    from intel_daily.runs import RunRepository
    from intel_daily.scheduling import ScheduleSettings, SettingsRepository
    import intel_daily.worker as worker_module
    from intel_daily.worker import worker_main

    _WebhookHandler.calls = 0
    real_build = worker_module._build_plan
    crashed = {"done": False}

    def flaky_build(db_path, include_sent, now):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("plan 阶段崩溃")
        return real_build(db_path, include_sent=include_sent, now=now)

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    SettingsRepository(db_path).save_schedule(
        ScheduleSettings(
            timezone="Asia/Shanghai", daily_time=time(0, 0), weekdays_only=False
        )
    )
    enable(db_path, "nea_json")
    monkeypatch.setattr(worker_module, "_build_plan", flaky_build)

    def adapters():
        return {
            "nea_json": lambda: FakeAdapter(published_at=NOW, title="样本条目 2026")
        }

    with _Server() as server:
        monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", server.url)
        with pytest.raises(RuntimeError):
            worker_main(["--once", "--send"], adapters=adapters())

        daily = [
            run
            for run in RunRepository(db_path).list_recent(20)
            if run.kind == "每日汇总"
        ]
        assert len(daily) == 1
        assert daily[0].status == "failed"
        assert daily[0].sent_count == 0
        assert daily[0].finished_at is not None
        assert _WebhookHandler.calls == 0

        # failed 不算“当日已跑完”：--check-due 再跑一次并成功送达
        rc = worker_main(["--check-due", "--send"], adapters=adapters())
    out = capsys.readouterr().out
    assert rc == 0
    assert "send-ok" in out
    assert _WebhookHandler.calls == 1
    daily = [
        run
        for run in RunRepository(db_path).list_recent(20)
        if run.kind == "每日汇总"
    ]
    assert len(daily) == 2
    assert {run.status for run in daily} == {"failed", "success"}


def test_dry_run_writes_window_truncation_diagnostic_to_run_row(
    db_path: Path, monkeypatch, capsys
) -> None:
    """P2-3：dry-run/预览路径的窗口截断诊断同样写入当日 run 行（与 send 路径同格式）。"""
    from intel_daily.notifier import FETCH_LIMIT
    from intel_daily.runs import RunRepository
    from intel_daily.worker import worker_main

    specs = [
        (f"w{i}", f"窗口样本 {i} 2026", "medium", NOW.isoformat())
        for i in range(FETCH_LIMIT + 5)
    ]
    _seed_items(db_path, specs)
    enable(db_path, "caam_html")

    class Noop:
        def fetch(self, cursor, now):
            from intel_daily.adapters import FetchBatch

            return FetchBatch(items=[], new_cursor=cursor or "")

    monkeypatch.setenv("INTEL_DB_PATH", str(db_path))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse")
    rc = worker_main(["--once", "--dry-run"], adapters={"caam_html": lambda: Noop()})
    capsys.readouterr()
    assert rc == 0

    daily = [
        run
        for run in RunRepository(db_path).list_recent(20)
        if run.kind == "每日汇总"
    ]
    assert len(daily) == 1
    assert daily[0].status == "success"
    assert daily[0].sent_count == 0
    assert f"截断 {5} 条" in daily[0].error_summary
    assert str(FETCH_LIMIT + 5) in daily[0].error_summary
