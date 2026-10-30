from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


def app_settings_for(tmp_path: Path):
    from intel_daily.config import AppSettings

    return AppSettings(
        db_path=tmp_path / "intel.db",
        admin_username="admin",
        admin_password="correct-horse",
    )


def test_next_due_uses_configured_timezone() -> None:
    from intel_daily.scheduling import ScheduleSettings, next_due_after

    settings = ScheduleSettings(
        timezone="Asia/Shanghai",
        daily_time=time(8, 30),
        weekdays_only=False,
    )
    now = datetime(2026, 8, 26, 0, 20, tzinfo=timezone.utc)

    assert next_due_after(now, settings) == datetime(
        2026,
        8,
        26,
        8,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )


def test_weekdays_only_skips_weekend() -> None:
    from intel_daily.scheduling import ScheduleSettings, next_due_after

    settings = ScheduleSettings(
        timezone="Asia/Shanghai",
        daily_time=time(8, 30),
        weekdays_only=True,
    )
    friday_after_run = datetime(
        2026,
        8,
        28,
        9,
        0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    assert next_due_after(friday_after_run, settings) == datetime(
        2026,
        8,
        31,
        8,
        30,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )


def test_schedule_form_persists_non_secret_settings(tmp_path: Path) -> None:
    from intel_daily.scheduling import SettingsRepository
    from intel_daily.web import create_app

    app = create_app(app_settings_for(tmp_path))
    response = TestClient(app).post(
        "/settings/schedule",
        data={
            "timezone": "Asia/Shanghai",
            "daily_time": "08:30",
            "weekdays_only": "true",
            "topics": "政策监管\n储能\n政策监管",
            "keywords": "碳酸锂\n动力电池",
            "exclude_words": "招聘",
        },
        auth=("admin", "correct-horse"),
        follow_redirects=False,
    )

    saved = SettingsRepository(tmp_path / "intel.db").get_schedule()

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert saved.daily_time == time(8, 30)
    assert saved.weekdays_only is True
    assert saved.topics == ("政策监管", "储能")
    assert saved.keywords == ("碳酸锂", "动力电池")
    assert saved.exclude_words == ("招聘",)


def test_settings_page_reports_secrets_without_exposing_them(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from intel_daily.web import create_app

    webhook = "https://secret.example/feishu-hook"
    api_key = "secret-ai-key"
    monkeypatch.setenv("INTEL_FEISHU_WEBHOOK", webhook)
    monkeypatch.setenv("INTEL_AI_API_KEY", api_key)
    app = create_app(app_settings_for(tmp_path))

    response = TestClient(app).get(
        "/settings",
        auth=("admin", "correct-horse"),
    )
    database_bytes = (tmp_path / "intel.db").read_bytes()

    assert response.status_code == 200
    assert response.text.count("已配置") >= 2
    assert webhook not in response.text
    assert api_key not in response.text
    assert webhook.encode() not in database_bytes
    assert api_key.encode() not in database_bytes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "Mars/Olympus"),
        ("daily_time", "25:61"),
        ("weekdays_only", "sometimes"),
    ],
)
def test_schedule_form_rejects_invalid_values(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    from intel_daily.web import create_app

    form_data = {
        "timezone": "Asia/Shanghai",
        "daily_time": "08:30",
        "weekdays_only": "false",
        "topics": "政策监管",
        "keywords": "储能",
        "exclude_words": "招聘",
    }
    form_data[field] = value
    app = create_app(app_settings_for(tmp_path))

    response = TestClient(app, raise_server_exceptions=False).post(
        "/settings/schedule",
        data=form_data,
        auth=("admin", "correct-horse"),
    )

    assert response.status_code == 422


def test_runs_page_orders_newest_first(tmp_path: Path) -> None:
    from intel_daily.runs import RunRepository
    from intel_daily.web import create_app

    app = create_app(app_settings_for(tmp_path))
    repository = RunRepository(tmp_path / "intel.db")
    repository.record_finished("2026-08-25T00:00:00Z", "success", 10, 4, 4, 0)
    repository.record_finished("2026-08-26T00:00:00Z", "partial", 8, 3, 0, 1)

    response = TestClient(app).get("/runs", auth=("admin", "correct-horse"))

    assert response.status_code == 200
    assert response.text.index("2026-08-26") < response.text.index("2026-08-25")
    assert "部分成功" in response.text


def test_runs_page_labels_failed_and_running(tmp_path: Path) -> None:
    from intel_daily.db import connect
    from intel_daily.runs import RunRepository
    from intel_daily.web import create_app

    app = create_app(app_settings_for(tmp_path))
    repository = RunRepository(tmp_path / "intel.db")
    repository.record_finished(
        "2026-08-26T01:00:00Z",
        "failed",
        3,
        0,
        0,
        3,
        error_summary="上游抓取超时",
    )
    with connect(tmp_path / "intel.db") as connection:
        connection.execute(
            """
            INSERT INTO runs (
                status,
                started_at,
                finished_at,
                collected_count,
                selected_count,
                sent_count,
                failed_count,
                error_summary,
                created_at
            ) VALUES ('running', '2026-08-26T02:00:00Z', NULL, 0, 0, 0, 0, '', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """
        )

    response = TestClient(app).get("/runs", auth=("admin", "correct-horse"))

    assert response.status_code == 200
    assert "失败" in response.text
    assert "运行中" in response.text


def test_runs_json_is_machine_readable_and_requires_auth(tmp_path: Path) -> None:
    from intel_daily.runs import RunRepository
    from intel_daily.web import create_app

    app = create_app(app_settings_for(tmp_path))
    repository = RunRepository(tmp_path / "intel.db")
    repository.record_finished(
        "2026-08-26T01:00:00Z",
        "partial",
        8,
        3,
        0,
        1,
        error_summary="上游响应慢",
    )

    assert TestClient(app).get("/runs.json").status_code == 401

    response = TestClient(app).get("/runs.json", auth=("admin", "correct-horse"))
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["status"] == "partial"
    assert data[0]["status_label"] == "部分成功"
    assert data[0]["error_summary"] == "上游响应慢"
