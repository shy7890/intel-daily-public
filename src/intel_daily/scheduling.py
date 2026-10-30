import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from intel_daily.db import connect


@dataclass(frozen=True)
class ScheduleSettings:
    timezone: str
    daily_time: time
    weekdays_only: bool
    topics: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclude_words: tuple[str, ...] = ()


def next_due_after(
    now: datetime,
    settings: ScheduleSettings,
) -> datetime:
    local_timezone = ZoneInfo(settings.timezone)
    local_now = now.astimezone(local_timezone)
    candidate = datetime.combine(
        local_now.date(),
        settings.daily_time,
        tzinfo=local_timezone,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    while settings.weekdays_only and candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


class SettingsRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def get_schedule(self) -> ScheduleSettings:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT
                    timezone,
                    daily_time,
                    weekdays_only,
                    topics_json,
                    keywords_json,
                    exclude_words_json
                FROM settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("settings row is missing")
        return ScheduleSettings(
            timezone=row["timezone"],
            daily_time=time.fromisoformat(row["daily_time"]),
            weekdays_only=bool(row["weekdays_only"]),
            topics=tuple(json.loads(row["topics_json"])),
            keywords=tuple(json.loads(row["keywords_json"])),
            exclude_words=tuple(json.loads(row["exclude_words_json"])),
        )

    def save_schedule(self, settings: ScheduleSettings) -> ScheduleSettings:
        with connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE settings
                SET
                    timezone = ?,
                    daily_time = ?,
                    weekdays_only = ?,
                    topics_json = ?,
                    keywords_json = ?,
                    exclude_words_json = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = 1
                """,
                (
                    settings.timezone,
                    settings.daily_time.strftime("%H:%M"),
                    int(settings.weekdays_only),
                    json.dumps(settings.topics, ensure_ascii=False),
                    json.dumps(settings.keywords, ensure_ascii=False),
                    json.dumps(settings.exclude_words, ensure_ascii=False),
                ),
            )
        return self.get_schedule()


def normalize_lines(value: str) -> tuple[str, ...]:
    normalized = (line.strip() for line in value.splitlines())
    return tuple(dict.fromkeys(line for line in normalized if line))
