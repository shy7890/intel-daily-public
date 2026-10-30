from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

from intel_daily.db import connect

STATUS_LABELS = {
    "success": "成功",
    "partial": "部分成功",
    "failed": "失败",
    "running": "运行中",
}


def started_at_for(now: datetime, started: float = 0.0) -> str:
    """由注入时钟 `now` 反推开始时刻（`started` 取 `monotonic()` 采样值）。

    直接用真实时钟相减会与注入的 `now` 不同源，测试/回放中会得到负耗时。
    """
    elapsed = (monotonic() - started) if started else 0.0
    return (now - timedelta(seconds=elapsed)).isoformat()


@dataclass(frozen=True)
class Run:
    id: int
    kind: str
    status: str
    scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    collected_count: int
    selected_count: int
    sent_count: int
    failed_count: int
    error_summary: str

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _run_from_row(row) -> Run:
    return Run(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        scheduled_for=_parse_datetime(row["scheduled_for"]),
        started_at=_parse_datetime(row["started_at"]),
        finished_at=_parse_datetime(row["finished_at"]),
        collected_count=row["collected_count"],
        selected_count=row["selected_count"],
        sent_count=row["sent_count"],
        failed_count=row["failed_count"],
        error_summary=row["error_summary"],
    )


class RunRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def record_finished(
        self,
        finished_at: str,
        status: str,
        collected_count: int,
        selected_count: int,
        sent_count: int,
        failed_count: int,
        error_summary: str = "",
        kind: str = "daily",
        started_at: str | None = None,
    ) -> None:
        """写入已结束的 run 行；`started_at` 可选。

        每日汇总走 `db.acquire_daily_lock`（开 run 即写 started_at）+
        `finish_run` 更新同一行；本方法用于信源/手动测试等一次性记录行。
        """
        with connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    kind,
                    status,
                    scheduled_for,
                    started_at,
                    finished_at,
                    collected_count,
                    selected_count,
                    sent_count,
                    failed_count,
                    error_summary,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    kind,
                    status,
                    None,
                    started_at,
                    finished_at,
                    collected_count,
                    selected_count,
                    sent_count,
                    failed_count,
                    error_summary,
                ),
            )

    def finish_run(
        self,
        run_id: int,
        finished_at: str,
        status: str,
        collected_count: int = 0,
        selected_count: int = 0,
        sent_count: int = 0,
        failed_count: int = 0,
        error_summary: str = "",
    ) -> None:
        """把 acquire_daily_lock 建立的 running 行更新为终态（同一行，不另插新行）。"""
        with connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?,
                    finished_at = ?,
                    collected_count = ?,
                    selected_count = ?,
                    sent_count = ?,
                    failed_count = ?,
                    error_summary = ?
                WHERE id = ?
                """,
                (
                    status,
                    finished_at,
                    collected_count,
                    selected_count,
                    sent_count,
                    failed_count,
                    error_summary,
                    run_id,
                ),
            )

    def list_recent(self, limit: int = 50) -> list[Run]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM runs
                ORDER BY
                    COALESCE(finished_at, started_at, created_at) DESC,
                    id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_run_from_row(row) for row in rows]
