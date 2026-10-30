import argparse
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from intel_daily.adapters.caam import CaamAdapter
from intel_daily.adapters.cninfo import CninfoAdapter
from intel_daily.adapters.comtrade import ComtradeAdapter
from intel_daily.adapters.cpca import CpcaAdapter
from intel_daily.adapters.gfex import GfexAdapter
from intel_daily.adapters.nea import NeaAdapter
from intel_daily.adapters.shfe import ShfeAdapter
from intel_daily.config import AppSettings
from intel_daily.db import (
    DAILY_RUN_KIND,
    acquire_daily_lock,
    init_database,
    release_daily_lock,
)
from intel_daily.digest import DeepSeekRankEngine, plan_entries, render_brief_limited
from intel_daily.similarity import DeepSeekSimilarityMerger
from intel_daily.notifier import (
    FETCH_LIMIT,
    FeishuWebhookNotifier,
    build_digest,
    count_window_items,
    fetch_items,
    render_card,
)
from intel_daily.items import ItemRepository
from intel_daily.pipeline import run_source_once
from intel_daily.runs import RunRepository, started_at_for
from intel_daily.scheduling import ScheduleSettings, SettingsRepository
from intel_daily.sources import SourceRepository
from intel_daily.summarizers import DeepSeekSummarizer, StubSummarizer


TEST_RUN_KIND = "手动测试"
ADAPTER_REGISTRY = {
    "nea_json": NeaAdapter,
    "cninfo_json": CninfoAdapter,
    "shfe_dat": ShfeAdapter,
    "gfex_json": GfexAdapter,
    "caam_html": CaamAdapter,
    "cpca_html": CpcaAdapter,
    "comtrade_json": ComtradeAdapter,
}

# 每日推送窗口（近 N 日新内容），诊断信息与 fetch_items 必须一致。
DIGEST_WINDOW_DAYS = 3
IMPORTANCE_LEVELS = ("high", "medium")

# 视为“当日已跑完”的运行状态：success/partial；failed/skipped/locked 不算。
DAILY_OK_STATUSES = ("success", "partial")


def daily_ok(status: str) -> bool:
    """运行状态是否算当日已完成（failed 不算，留出 T12 的当日重试）。"""
    return status in DAILY_OK_STATUSES


@dataclass(frozen=True)
class WorkerResult:
    status: str
    unsupported: int
    failures: tuple[str, ...]
    ran_on: str | None
    # 当日“每日汇总”run 行 id：send 模式为未收尾的重入锁行，
    # 预览/试运行路径为 run_daily 已收尾的行（仅供补写窗口诊断）；
    # skipped/locked/未建立锁行时为 None。
    run_id: int | None = None


@dataclass(frozen=True)
class TestSendResult:
    ok: bool
    status: str
    message: str
    chars: int
    sent_count: int = 0


def daily_window_due(
    now: datetime,
    settings: ScheduleSettings,
    last_run_at: datetime | None,
) -> bool:
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    if settings.weekdays_only and local_now.weekday() >= 5:
        return False
    scheduled = datetime.combine(local_now.date(), settings.daily_time, tzinfo=tz)
    if now < scheduled:
        return False
    if last_run_at is not None and last_run_at >= scheduled:
        return False
    return True


def run_daily(
    db_path: Path,
    adapters,
    now: datetime,
    summarizer=None,
    force: bool = False,
    hold_lock: bool = False,
) -> WorkerResult:
    """执行当日信源采集。

    `hold_lock=True` 时重入锁（“每日汇总”running 行）保持占用、不写终态，
    由调用方（worker CLI 的推送阶段）用 `finish_daily_run` 收尾，
    使锁覆盖“采集 + 推送”全程，杜绝并发双 worker 双发。
    """
    if not force and _already_ran_today(db_path, now):
        return WorkerResult(status="skipped", unsupported=0, failures=(), ran_on=None)

    started = monotonic()
    run_id: int | None = None
    if not force:
        run_id = acquire_daily_lock(
            db_path,
            run_date=_today_label(db_path, now),
            started_at=started_at_for(now, started),
            kind=DAILY_RUN_KIND,
        )
        if run_id is None:
            return WorkerResult(status="locked", unsupported=0, failures=(), ran_on=None)

    repository = SourceRepository(db_path)
    enabled = [source for source in repository.list_all() if source.enabled]
    unsupported = 0
    failure_messages: list[str] = []
    ran_any = False
    successes = 0
    try:
        for source in enabled:
            factory = adapters.get(source.adapter_type)
            if factory is None:
                unsupported += 1
                continue
            result = run_source_once(
                db_path, source, factory(), now, summarizer=summarizer
            )
            ran_any = True
            if result.status in ("success", "partial"):
                successes += 1
            if result.status in ("success", "partial") and result.new_cursor:
                repository.advance_success(
                    source.id, result.new_cursor, now.isoformat()
                )
            if result.failed or result.status == "failed":
                detail = result.error or result.status
                failure_messages.append(f"{source.name}: {detail}")
    except Exception as error:
        # 循环本身的意外崩溃（信源表被并发改动、适配器构造失败等）：
        # 先把锁行写成 failed 再抛出，避免残留 running 把当日后续运行挡满 2 小时。
        if run_id is not None:
            finish_daily_run(
                db_path,
                run_id,
                now,
                "failed",
                tuple(failure_messages)
                + (f"每日汇总异常: {type(error).__name__}: {error}",),
            )
        raise
    status = _overall_status(ran_any, failure_messages, successes)

    if not ran_any:
        if run_id is not None:
            # 当日无事可做：释放锁，不得留下 running 行阻塞后续重试。
            release_daily_lock(db_path, run_id)
        return WorkerResult(
            status=status,
            unsupported=unsupported,
            failures=tuple(failure_messages),
            ran_on=None,
            run_id=None,
        )

    if force:
        _record_daily_run(
            db_path,
            now,
            status,
            failure_messages,
            kind=TEST_RUN_KIND,
            started=started,
        )
    else:
        if hold_lock:
            # 锁行保持 running（覆盖后续推送阶段），由 finish_daily_run 写终态。
            pass
        else:
            finish_daily_run(
                db_path, run_id, now, status, tuple(failure_messages)
            )
            # 行已收尾，但 id 仍随结果返回：预览/试运行路径据此把
            # 窗口截断诊断补写进同一行（P2-3）。
        from intel_daily.maintenance import prune_expired

        print(prune_expired(db_path))
    return WorkerResult(
        status=status,
        unsupported=unsupported,
        failures=tuple(failure_messages),
        ran_on=now.isoformat(),
        run_id=run_id,
    )


def finish_daily_run(
    db_path: Path,
    run_id: int,
    now: datetime,
    status: str,
    failures: tuple[str, ...] = (),
    sent_count: int = 0,
    diagnostic: str = "",
) -> None:
    """把 hold_lock 保留的 running 行更新为终态，并写入真实发送条数与诊断。"""
    error_summary = "; ".join(failures)
    if diagnostic:
        error_summary = f"{error_summary}; {diagnostic}" if error_summary else diagnostic
    RunRepository(db_path).finish_run(
        run_id,
        finished_at=now.isoformat(),
        status=status,
        collected_count=0,
        selected_count=0,
        sent_count=sent_count,
        failed_count=len(failures),
        error_summary=error_summary[:1000],
    )


def _already_ran_today(db_path: Path, now: datetime) -> bool:
    """当日是否已跑过：ok（success/partial）即视为已跑；全源 failed 允许重试一次。"""
    settings = SettingsRepository(db_path).get_schedule()
    tz = ZoneInfo(settings.timezone)
    local_date = now.astimezone(tz).date()
    runs = RunRepository(db_path)
    failed_today = 0
    for run in runs.list_recent(200):
        if run.kind != DAILY_RUN_KIND or run.finished_at is None:
            continue
        if run.finished_at.astimezone(tz).date() != local_date:
            continue
        if daily_ok(run.status):
            return True
        if run.status == "failed":
            failed_today += 1
    # 全源失败的运行不算“当日已跑完”，但只允许再试一次，避免每轮 cron 反复打上游。
    return failed_today >= 2


def _overall_status(ran_any: bool, failures: list[str], successes: int = 0) -> str:
    """全源失败=failed（当日可重试一次）；部分失败=partial；无失败=success。"""
    if not ran_any:
        return "skipped"
    if successes == 0:
        return "failed"
    if failures:
        return "partial"
    return "success"


def _record_daily_run(
    db_path: Path,
    now: datetime,
    status: str,
    failures: list[str],
    kind: str = DAILY_RUN_KIND,
    started: float = 0.0,
) -> None:
    RunRepository(db_path).record_finished(
        finished_at=now.isoformat(),
        status=status,
        collected_count=0,
        selected_count=0,
        sent_count=0,
        failed_count=len(failures),
        error_summary="; ".join(failures)[:1000],
        kind=kind,
        started_at=started_at_for(now, started),
    )


def build_summarizer_from_env() -> DeepSeekSummarizer | StubSummarizer:
    api_key = os.getenv("INTEL_AI_API_KEY")
    if not api_key:
        return StubSummarizer()
    return DeepSeekSummarizer(
        api_key=api_key,
        model=os.getenv("INTEL_AI_MODEL", "deepseek-chat"),
        base_url=os.getenv("INTEL_AI_BASE_URL", "https://api.deepseek.com"),
    )


def _build_plan(db_path: Path, include_sent: bool, now: datetime):
    """取窗口条目 → 排序/合并 → 渲染，返回 (plan, (brief, trim_stats), date_label, fetched)。

    `fetched` 为实际取到的条数（受 fetch_items 的 LIMIT 约束），
    调用方可与 `count_window_items` 对比得出被截断的数量。
    """
    date_label = _today_label(db_path, now)
    items = fetch_items(
        db_path, days=DIGEST_WINDOW_DAYS, include_sent=include_sent, now=now
    )
    api_key = os.getenv("INTEL_AI_API_KEY")
    engine = DeepSeekRankEngine(api_key=api_key) if api_key else None
    merger = DeepSeekSimilarityMerger(api_key=api_key) if api_key else None
    plan = plan_entries(
        items, ai=engine, merger=merger, importance_levels=IMPORTANCE_LEVELS
    )
    return (
        plan,
        render_brief_limited(plan, date_label=date_label),
        date_label,
        len(items),
    )


def _today_label(db_path: Path, now: datetime) -> str:
    schedule = SettingsRepository(db_path).get_schedule()
    return now.astimezone(ZoneInfo(schedule.timezone)).date().isoformat()


def _window_diagnostic(db_path: Path, now: datetime, fetched: int) -> str:
    """窗口内条目多于实际取到的条数时生成诊断文本（受 LIMIT 截断不得静默）。"""
    total = count_window_items(db_path, days=DIGEST_WINDOW_DAYS, now=now)
    truncated = max(0, total - fetched)
    if not truncated:
        return ""
    return (
        f"近 {DIGEST_WINDOW_DAYS} 日窗口共 {total} 条，"
        f"受 LIMIT {FETCH_LIMIT} 截断 {truncated} 条"
    )


def mark_digest_sent(db_path: Path, plan, trim_stats) -> list[int]:
    """推送成功后标记“真正进入卡片”的条目为 sent，返回被标记的 id。

    规则（D-012 之外的每日进度语义）：
    - 只有渲染存活条目（trim_stats.kept_ids）算已推送；被 20K 裁剪掉的条目
      必须保留 collected，下一日照常参与排序；
    - canonical 存活时，其被相似合并吸收的替身（含链式传递吸收）一并标 sent，
      否则次日替身重复推送；吸收链的根未存活时，整条链同样不动。
    """
    survivors = set(trim_stats.kept_ids)
    ids = {entry.id for entry in plan.total if entry.id is not None} & survivors
    # 链式吸收闭包：B→A 确定性合并、A→C AI 合并产生 pairs=[(A,B),(C,A)]，
    # A 已不在存活集（被 C 吸收）但仍是 B 的吸收方。不动点迭代：
    # canonical 在存活集或已在闭包内，其全部传递吸收者一并标 sent，
    # 否则中间替身残留 collected、次日重复推送。
    changed = True
    while changed:
        changed = False
        for canonical_id, absorbed_id in plan.absorbed_of:
            if absorbed_id not in ids and (
                canonical_id in survivors or canonical_id in ids
            ):
                ids.add(absorbed_id)
                changed = True
    marked = sorted(ids)
    if marked:
        ItemRepository(db_path).mark_sent(marked)
    return marked


def perform_test_send(
    db_path: Path, adapters=None, now: datetime | None = None
) -> TestSendResult:
    """测试发送：强制执行采集与摘要，并把当日内含已推送条目发往飞书测试群。

    与每日汇总互不干扰：Run 记录 kind='手动测试'，不占用当日汇总进度；
    已发送条目通过 include_sent 重新纳入 digest，便于一天内重复测试。
    """
    webhook = os.getenv("INTEL_FEISHU_WEBHOOK")
    if not webhook:
        return TestSendResult(
            ok=False,
            status="error",
            message="未配置 INTEL_FEISHU_WEBHOOK，无法测试发送",
            chars=0,
        )
    now = now or datetime.now(timezone.utc)
    try:
        run_daily(
            db_path,
            adapters or ADAPTER_REGISTRY,
            now,
            summarizer=build_summarizer_from_env(),
            force=True,
        )
        plan, (brief, trim_stats), date_label, _fetched = _build_plan(
            db_path, include_sent=True, now=now
        )
        if not plan.total:
            return TestSendResult(
                ok=True,
                status="skip",
                message="无可用情报，未发送",
                chars=trim_stats.after_chars,
            )
        receipt = FeishuWebhookNotifier(webhook).send_card(
            render_card(brief, date_label=date_label)
        )
    except Exception as error:
        return TestSendResult(
            ok=False, status="error", message=f"测试发送失败: {error}", chars=0
        )
    if not receipt.ok:
        return TestSendResult(
            ok=False,
            status="error",
            message=receipt.error or "飞书发送失败",
            chars=trim_stats.after_chars,
        )
    ids = [entry.id for entry in plan.total if entry.id is not None]
    # D-012：测试发送不占每日进度，绝不改变条目状态（不 mark_sent），
    # 09:00 正式 digest 仍须包含这些条目。
    return TestSendResult(
        ok=True,
        status="sent",
        message=f"已发送 {len(ids)} 条 · {trim_stats.after_chars} 字符",
        chars=trim_stats.after_chars,
        sent_count=len(ids),
    )


def worker_main(argv: list[str] | None = None, adapters=None) -> int:
    parser = argparse.ArgumentParser(
        prog="intel-daily-worker",
        description="新能源每日情报 worker：执行当日信源采集与摘要",
    )
    parser.add_argument(
        "--once", action="store_true", help="立即执行一次（不检查到期）"
    )
    parser.add_argument(
        "--check-due", action="store_true", help="仅在当日窗口到期时执行"
    )
    parser.add_argument(
        "--preview", action="store_true", help="打印今日情报预览（不发送）"
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="把今日情报文本发送到飞书（需要 INTEL_FEISHU_WEBHOOK 环境变量）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="渲染卡片但绝不发送（样式/内容核对用，不调用 webhook）",
    )
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="临时测试发送：强制采集+摘要（含已推送内容）到飞书测试群，"
        "无视当日窗口/运行进度；与 --dry-run 同用时以 dry-run 为准",
    )
    args = parser.parse_args(argv)

    app_settings = AppSettings.from_env()
    db_path = app_settings.db_path
    init_database(db_path)
    adapters = adapters or ADAPTER_REGISTRY
    now = datetime.now(timezone.utc)

    if args.check_due:
        schedule = SettingsRepository(db_path).get_schedule()
        if not daily_window_due(now, schedule, _last_daily_ok_finished(db_path)):
            print("daily-skip: 未到当日窗口")
            return 0

    if args.test_send and not args.dry_run and not args.preview:
        test_result = perform_test_send(db_path, adapters=adapters, now=now)
        if test_result.status == "sent":
            print(f"test-send-ok chars={test_result.chars}")
        else:
            print(f"send-{test_result.status}: {test_result.message}")
        return 0 if test_result.ok else 1

    # dry-run 永远优先于发送；--preview --send 仍按老语义：预览后照常发送。
    sending = args.send and not args.dry_run
    result = run_daily(
        db_path,
        adapters,
        now,
        summarizer=build_summarizer_from_env(),
        hold_lock=sending,
    )
    outcome = (
        f"daily-{result.status} "
        f"unsupported={result.unsupported} "
        f"failures={len(result.failures)} "
        f"ran_on={result.ran_on or '-'}"
    )
    print(outcome)
    if result.status == "locked":
        # 已有每日汇总在运行（重入锁未释放）：本轮跳过，绝不双开双发。
        print("daily-locked: 已有每日汇总在运行，跳过本轮")
        return 0

    finalized = False

    def finalize(status: str, sent_count: int, diagnostic: str = "") -> None:
        """把 hold_lock 保留的 running 锁行写成终态（幂等，只收尾一次）。"""
        nonlocal finalized
        if result.run_id is None or finalized:
            return
        finalized = True
        finish_daily_run(
            db_path,
            result.run_id,
            now,
            status,
            result.failures,
            sent_count=sent_count,
            diagnostic=diagnostic,
        )

    try:
        plan, (brief, trim_stats), date_label, fetched = _build_plan(
            db_path, include_sent=False, now=now
        )
        print(
            f"brief-chars={trim_stats.after_chars} "
            f"trimmed={trim_stats.trimmed}"
        )
        diagnostic = _window_diagnostic(db_path, now, fetched)
        if not sending:
            # 预览/试运行不推送：run 行已由 run_daily 收尾，这里把窗口诊断
            # 补写进同一行（与 send 路径同格式，sent_count 保持 0）。
            finalize(result.status, 0, diagnostic)

        if args.preview:
            print(brief)
        if args.dry_run:
            render_card(brief, date_label=date_label)
            print("send-dry-run 卡片已生成并打印，未发送（不调用 webhook）")
            return 0
        if sending:
            webhook = os.getenv("INTEL_FEISHU_WEBHOOK")
            if not webhook:
                print("send-error: 未配置 INTEL_FEISHU_WEBHOOK，已跳过发送")
                finalize("failed", 0, diagnostic)
                return 1
            if not plan.total:
                print("send-skip: 无可用情报，未发送")
                finalize(result.status, 0, diagnostic)
                return 0
            try:
                receipt = FeishuWebhookNotifier(webhook).send_card(
                    render_card(brief, date_label=date_label)
                )
                marked = (
                    mark_digest_sent(db_path, plan, trim_stats)
                    if receipt.ok
                    else []
                )
            except Exception as error:
                # 推送阶段的意外异常同样要收尾锁行，否则当日残留 running 阻塞重试。
                print(f"send-error: {type(error).__name__}: {error}")
                finalize("failed", 0, diagnostic)
                return 1
            suffix = "" if receipt.ok else f" error={receipt.error}"
            print(f"send-{'ok' if receipt.ok else 'error'}{suffix}")
            # 送达失败记 failed：采集成功但没送达不等于当日任务完成（T12 允许重试一次）。
            finalize(
                result.status if receipt.ok else "failed",
                len(marked),
                diagnostic,
            )
            if not receipt.ok:
                return 1
        return 0 if result.status in ("success", "skipped") else 1
    except BaseException:
        # 意外崩溃穿出（plan/渲染/发送前的异常）：锁行一律以 failed 写终态。
        # 绝不能沿用 result.status=success/sent=0——那会把未完成的当日记成
        # 已跑完，挡掉当日重试（P2-2）。
        finalize("failed", 0, "")
        raise
    finally:
        # 兜底收尾：所有正常返回路径都已显式 finalize；此处防漏，
        # 绝不把重入锁行留在 running。
        finalize(result.status, 0, "")


def _last_daily_ok_finished(db_path: Path) -> datetime | None:
    """最近一次“算已完成”的每日汇总结束时刻。

    failed 不算已跑：全源失败当天窗口仍然到期（T12），
    当日重试次数由 `_already_ran_today` 兜底限制为一次。
    """
    for run in RunRepository(db_path).list_recent(200):
        if (
            run.kind == DAILY_RUN_KIND
            and run.finished_at is not None
            and daily_ok(run.status)
        ):
            return run.finished_at
    return None


if __name__ == "__main__":
    raise SystemExit(worker_main())
