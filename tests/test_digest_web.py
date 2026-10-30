from pathlib import Path

from fastapi.testclient import TestClient


def _settings(tmp_path: Path):
    from intel_daily.config import AppSettings

    return AppSettings(
        db_path=tmp_path / "intel.db",
        admin_username="admin",
        admin_password="correct-horse",
    )


def test_digest_page_requires_auth(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(_settings(tmp_path))
    assert TestClient(app).get("/digest").status_code == 401


def test_digest_page_lists_items_by_block(tmp_path: Path) -> None:
    import sqlite3

    from intel_daily.web import create_app

    db_path = tmp_path / "intel.db"
    app = create_app(_settings(tmp_path))
    from datetime import datetime, timezone, timedelta

    # 相对日期：digest 页走真实时钟窗口，保证未来任意一天运行均在近 3 日内
    published_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO intelligence_items (
                source_id, external_id, title, original_url, canonical_url,
                publisher, published_at, fetched_at, source_type,
                verification_status, category, importance, summary,
                content_fingerprint, status
            ) VALUES (3, 'd1', ?, ?, ?, ?, ?, ?, 'primary',
                      'primary', '储能', 'high', ?, 'sha256:d1', 'collected')
            """,
            (
                "储能试点政策发布",
                "http://www.nea.gov.cn/20260826/d1/c.html",
                "http://www.nea.gov.cn/20260826/d1/c.html",
                "国家能源局",
                published_at,
                published_at,
                "储能政策摘要。",
            ),
        )

    response = TestClient(app).get("/digest", auth=("admin", "correct-horse"))
    assert response.status_code == 200
    assert "今日情报" in response.text
    assert "新能源与储能" in response.text
    assert "储能试点政策发布" in response.text
    assert "http://www.nea.gov.cn/20260826/d1/c.html" in response.text


def test_digest_page_date_label_uses_configured_timezone(
    tmp_path: Path, monkeypatch
) -> None:
    """T10：页面“今日情报（日期）”标签按 settings.timezone，而非 UTC 日期。"""
    from datetime import datetime, time, timezone

    from intel_daily.scheduling import ScheduleSettings, SettingsRepository
    from intel_daily.web import create_app

    app = create_app(_settings(tmp_path))
    db_path = tmp_path / "intel.db"
    SettingsRepository(db_path).save_schedule(
        ScheduleSettings(
            timezone="Pacific/Kiritimati",  # UTC+14：与 UTC 日历日常常差一天
            daily_time=time(9, 0),
            weekdays_only=False,
        )
    )
    # UTC 12:00 时，UTC+14 已是次日
    monkeypatch.setattr(
        "intel_daily.web._utc_now",
        lambda: datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )

    text = TestClient(app).get("/digest", auth=("admin", "correct-horse")).text

    assert "今日情报（2026-08-27）" in text
    assert "今日情报（2026-08-26）" not in text
