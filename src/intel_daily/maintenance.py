from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from intel_daily.db import connect


@dataclass(frozen=True)
class PruneReport:
    items_unpushed: int
    items_selected: int
    runs: int
    detail: str

    @property
    def keeps_sent(self) -> bool:
        return True


def prune_expired(
    db_path: Path,
    unpushed_days: int = 30,
    selected_days: int = 365,
    run_days: int = 365,
) -> str:
    """清理过期情报与运行记录（D-012 之后的生命周期规则）：

    - collected/skipped/failed：`unpushed_days`（默认 30 天）到期的未推送行删除；
    - selected：`selected_days`（默认 365 天）到期删除；
    - sent：**永不删除**（与 Miniflux tombstone 同构，防已推内容重发）；
    - runs：`run_days`（默认 365 天）到期删除。
    返回一行人类/agent 可读摘要。
    """
    now = datetime.now(timezone.utc)
    unpushed_cut = (now - timedelta(days=unpushed_days)).isoformat()
    selected_cut = (now - timedelta(days=selected_days)).isoformat()
    runs_cut = (now - timedelta(days=run_days)).isoformat()
    report = PruneReport(0, 0, 0, "")
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            DELETE FROM intelligence_items
            WHERE status IN ('collected', 'skipped', 'failed')
              AND fetched_at < ?
            """,
            (unpushed_cut,),
        )
        report = report.__class__(
            report.items_unpushed + cursor.rowcount,
            report.items_selected,
            report.runs,
            report.detail,
        )
        cursor = connection.execute(
            """
            DELETE FROM intelligence_items
            WHERE status = 'selected'
              AND fetched_at < ?
            """,
            (selected_cut,),
        )
        report = report.__class__(
            report.items_unpushed,
            report.items_selected + cursor.rowcount,
            report.runs,
            report.detail,
        )
        cursor = connection.execute(
            """
            DELETE FROM runs
            WHERE finished_at IS NOT NULL
              AND finished_at < ?
            """,
            (runs_cut,),
        )
        report = report.__class__(
            report.items_unpushed,
            report.items_selected,
            report.runs + cursor.rowcount,
            report.detail,
        )
    return (
        "prune: collected/skipped 删除了 "
        f"{report.items_unpushed} 条；selected 删除了 {report.items_selected} 条；"
        f"runs 删除了 {report.runs} 条（sent 永不删除）"
    )
