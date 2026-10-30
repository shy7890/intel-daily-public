from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic

from intel_daily.adapters.contract import FetchBatch
from intel_daily.adapters.nea import SourceFetchError
from intel_daily.classify import classify_text
from intel_daily.gate import evidence_passes
from intel_daily.items import ItemRepository
from intel_daily.runs import RunRepository, started_at_for
from intel_daily.sources import Source


@dataclass(frozen=True)
class PipelineResult:
    collected: int
    deduplicated: int
    skipped: int
    failed: int
    new_cursor: str
    status: str
    error: str = ""


def run_source_once(
    db_path: Path,
    source: Source,
    adapter,
    now: datetime,
    summarizer=None,
) -> PipelineResult:
    started = monotonic()
    try:
        try:
            batch = adapter.fetch(cursor=source.last_success_cursor, now=now)
        except SourceFetchError as error:
            result = PipelineResult(
                collected=0,
                deduplicated=0,
                skipped=0,
                failed=1,
                new_cursor=source.last_success_cursor or "",
                status="failed",
                error=str(error),
            )
        else:
            result = _process_batch(
                db_path, source, batch, summarizer=summarizer
            )
    except Exception as error:
        # 单个信源的意外异常（脏数据、解析崩溃等）必须就地隔离：
        # 记 failed 并继续其余信源，否则一个源会中断整个每日采集。
        result = PipelineResult(
            collected=0,
            deduplicated=0,
            skipped=0,
            failed=1,
            new_cursor=source.last_success_cursor or "",
            status="failed",
            error=f"{type(error).__name__}: {error}"[:1000],
        )
    _record_run(db_path, source, result, now, started)
    return result


def _record_run(db_path, source, result, now, started: float) -> None:
    try:
        RunRepository(db_path).record_finished(
            finished_at=now.isoformat(),
            status=result.status,
            collected_count=result.collected,
            selected_count=0,
            sent_count=0,
            failed_count=result.failed,
            error_summary=result.error,
            kind=source.name,
            started_at=started_at_for(now, started),
        )
    except Exception as error:
        # 运行记录失败不得反过来中断采集主流程（诊断信息打印即可）。
        print(f"run-record-failed: {source.name}: {type(error).__name__}: {error}")


def _process_batch(
    db_path: Path,
    source: Source,
    batch: FetchBatch,
    summarizer=None,
) -> PipelineResult:
    repository = ItemRepository(db_path)
    collected = 0
    deduplicated = 0
    skipped = 0
    failed = 0
    summary_errors: list[str] = []
    allow_ai = source.allow_external_ai and summarizer is not None
    for item in batch.items:
        classification = classify_text(
            item.title, item.summary, item.keywords
        )
        outcome = repository.upsert(
            source.id,
            item,
            category=classification.category,
            importance=classification.importance,
            source_type=source.source_type,
        )
        if outcome == "inserted":
            collected += 1
        elif outcome == "duplicate":
            deduplicated += 1
        else:
            skipped += 1
        row = repository.get_by_external_id(source.id, item.external_id)
        gate = evidence_passes(
            title=item.title,
            original_url=item.original_url,
            published_at=item.published_at.isoformat(),
            publisher=item.publisher,
        )
        if not gate.passed:
            repository.set_verification_status(row["id"], "secondary_pending")
            skipped += 1
            continue
        if not allow_ai:
            continue
        summary_result = summarizer.generate(
            f"{item.title}\n{item.summary}"
        )
        if summary_result.summary:
            repository.update_summary(row["id"], summary_result.summary)
        elif summary_result.provider == "stub":
            pass
        else:
            failed += 1
            summary_errors.append(summary_result.error or "摘要失败")
    status = "failed" if failed > 0 and collected == 0 else (
        "partial" if failed > 0 else "success"
    )
    return PipelineResult(
        collected=collected,
        deduplicated=deduplicated,
        skipped=skipped,
        failed=failed,
        new_cursor=batch.new_cursor,
        status=status,
        error="; ".join(summary_errors)[:1000],
    )
