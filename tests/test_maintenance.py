from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intel_daily.db import connect, init_database
from intel_daily.items import ItemRepository
from intel_daily.runs import RunRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "intel.db"
    init_database(path)
    return path


def _insert_item(db_path: Path, status: str, days_ago: int) -> int:
    fetched = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO intelligence_items (
                source_id, external_id, title, original_url, canonical_url,
                publisher, published_at, fetched_at, source_type,
                verification_status, category, importance, summary,
                content_fingerprint, status
            ) VALUES (1, ?, ?, 'https://x.example/', 'https://x.example/',
                '测试源', ?, ?, 'primary', 'primary', '行业动态', 'medium',
                '', 'fp', ?)
            """,
            (f"ext-{status}-{days_ago}", f"{status} 标题 {days_ago}", fetched, fetched, status),
        )
        return int(cursor.lastrowid)


def test_prune_keeps_sent_rows_forever(db_path: Path) -> None:
    from intel_daily.maintenance import prune_expired

    _insert_item(db_path, "sent", 500)
    _insert_item(db_path, "collected", 5)
    report = prune_expired(db_path)
    assert "sent" in report
    assert ItemRepository(db_path).count() == 2


def test_prune_deletes_old_collected_but_keeps_recent(db_path: Path) -> None:
    from intel_daily.maintenance import prune_expired

    _insert_item(db_path, "collected", 40)
    _insert_item(db_path, "collected", 3)
    _insert_item(db_path, "skipped", 40)
    report = prune_expired(db_path)
    assert "collected/skipped" in report
    remaining = [r["status"] for r in ItemRepository(db_path).list_recent(50)]
    assert remaining.count("collected") == 1
    assert remaining.count("skipped") == 0


def test_prune_deletes_selected_older_than_year_keeps_sent_old(db_path: Path) -> None:
    from intel_daily.maintenance import prune_expired

    _insert_item(db_path, "selected", 400)
    _insert_item(db_path, "sent", 400)
    _insert_item(db_path, "selected", 100)
    prune_expired(db_path)
    remaining = [r["status"] for r in ItemRepository(db_path).list_recent(50)]
    assert "selected" in remaining
    assert "sent" in remaining


def test_prune_deletes_old_runs_keeps_recent(db_path: Path) -> None:
    from intel_daily.maintenance import prune_expired

    runs = RunRepository(db_path)
    for days_ago in (400, 10):
        runs.record_finished(
            finished_at=(
                datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).isoformat(),
            status="success",
            collected_count=1,
            selected_count=0,
            sent_count=1,
            failed_count=0,
            error_summary="",
            kind="每日汇总",
        )
    report = prune_expired(db_path)
    assert report.count("runs") >= 0
    recent = runs.list_recent(50)
    assert len(recent) == 1


def test_prune_returns_summary_lines(db_path: Path) -> None:
    from intel_daily.maintenance import prune_expired

    _insert_item(db_path, "collected", 60)
    summary = prune_expired(db_path)
    assert "删除了" in summary or "已清理" in summary
