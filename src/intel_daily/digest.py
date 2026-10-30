import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from intel_daily.similarity import merge_similar

IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}
IMPORTANCE_LABELS = {"high": "高", "medium": "中", "low": "低"}

BLOCKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("政策与监管", ("私募监管", "宏观政策")),
    ("新能源与储能", ("储能", "新能源", "电力市场")),
    ("材料与产业链", ("产业链",)),
    ("公司动态", ("公司动态",)),
    ("行业动态", ("行业动态",)),
)

MAX_TOTAL_DEFAULT = 12
_PER_BLOCK_DEFAULT = 3
DEFAULT_MAX_CHARS = 20000
_ALL_IMPORTANCE_LEVELS = ("high", "medium", "low")
DEMO_MARKERS = ("样式预览", "示例摘要")
TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DigestEntry:
    title: str
    summary: str
    block: str
    importance: str
    url: str
    publisher: str
    published_at: str
    id: int | None = None
    note: str = ""
    related_links: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class DigestPlan:
    total: tuple[DigestEntry, ...]
    grouped: dict[str, tuple[DigestEntry, ...]]
    skimmed: bool
    # 相似合并中被吸收（丢弃）的替身条目 id 及其 canonical 归属，
    # 发送成功后须与存活的 canonical 一并 mark_sent，防止次日重复推送。
    absorbed_ids: tuple[int, ...] = ()
    absorbed_of: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class TrimStats:
    trimmed: int
    before_chars: int
    after_chars: int
    within_limit: bool
    # 实际渲染存活的条目 id 集合：字符裁剪掉的、以及“同类另有 N 条”溢出
    # 未渲染的条目都不在其中，worker 只对照存活集 mark_sent。
    kept_ids: frozenset[int] = frozenset()


def block_of(category: str) -> str:
    for name, markers in BLOCKS:
        if category in markers:
            return name
    return "行业动态"


def plan_entries(
    rows: list[dict],
    ai=None,
    merger=None,
    max_total: int = MAX_TOTAL_DEFAULT,
    importance_levels: tuple[str, ...] = _ALL_IMPORTANCE_LEVELS,
) -> DigestPlan:
    usable = [
        row
        for row in rows
        if (row.get("original_url") or "").strip()
        and not _is_demo(row)
    ]
    base = [
        _to_entry(row)
        for row in usable
        if (row.get("importance") or "low") in importance_levels
    ]
    absorbed_ids: tuple[int, ...] = ()
    absorbed_of: tuple[tuple[int, int], ...] = ()
    if merger is not None:
        merged = merge_similar(base, ai=merger)
        base = list(merged.entries)
        absorbed_ids = merged.absorbed_ids
        absorbed_of = merged.absorbed_of
    if not base:
        return DigestPlan(
            total=(),
            grouped={},
            skimmed=False,
            absorbed_ids=absorbed_ids,
            absorbed_of=absorbed_of,
        )
    if ai is not None:
        ranked = ai.rank(base)
        if ranked is not None:
            returns = ranked[:max_total]
            grouped = _group(returns)
            return DigestPlan(
                total=tuple(returns),
                grouped=grouped,
                skimmed=True,
                absorbed_ids=absorbed_ids,
                absorbed_of=absorbed_of,
            )
    # AI 缺失/失败兜底：按重要度升序、同级“最新优先”
    ranked = sorted(base, key=lambda e: e.published_at or "", reverse=True)
    ranked = sorted(ranked, key=lambda e: IMPORTANCE_ORDER.get(e.importance, 2))
    returns = ranked[:max_total]
    return DigestPlan(
        total=tuple(returns),
        grouped=_group(returns),
        skimmed=True,
        absorbed_ids=absorbed_ids,
        absorbed_of=absorbed_of,
    )


def _group(entries: list[DigestEntry]) -> dict[str, tuple[DigestEntry, ...]]:
    grouped: dict[str, list[DigestEntry]] = {}
    order = [b[0] for b in BLOCKS]
    for entry in entries:
        grouped.setdefault(entry.block, []).append(entry)
    return {name: tuple(grouped[name]) for name in order if name in grouped}


def _to_entry(row: dict) -> DigestEntry:
    note = row.get("note") or ""
    return DigestEntry(
        related_links=tuple(row.get("related_links") or ()),
        title=row.get("title") or "",
        summary=row.get("summary") or "",
        block=block_of(row.get("category") or ""),
        importance=row.get("importance") or "low",
        url=row.get("original_url") or "",
        publisher=row.get("publisher") or "",
        published_at=row.get("published_at") or "",
        id=row.get("id"),
        note=note,
    )


class RankEngine:
    """AI 重要度/排序引擎契约：返回排序后的条目列表（含 note 注记），失败返回 None。"""

    def __init__(self, rank_fn) -> None:
        self.rank_fn = rank_fn

    def rank(self, entries: list[DigestEntry]) -> list[DigestEntry] | None:
        return self.rank_fn(entries)


_RANK_SYSTEM_PROMPT = (
    "你是公司新能源投资情报筛选助手。根据下列条目（编号即索引），判断每条对"
    "新能源投资团队的重要性，并输出精选序列。规则：只保留高/中重要度条目；"
    "同主题头条优先；政策与监管动态要突出；重复或平淡的条目不要选。最后只输出"
    "JSON（不要任何其他文字）：{\"ranking\": [按优先级从高到低的索引数组], "
    "\"notes\": {\"索引\": \"15字以内的一句话要点\"}}"
)


class DeepSeekRankEngine:
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 60,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def rank(self, entries: list[DigestEntry]) -> list[DigestEntry] | None:
        if not entries:
            return []
        import json
        import urllib.error
        import urllib.request

        items = [
            {
                "i": i,
                "title": e.title[:80],
                "summary": e.summary[:120],
                "block": e.block,
                "published_at": e.published_at[:10],
            }
            for i, e in enumerate(entries)
        ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _RANK_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(items, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "intel-daily/0.1 (+internal daily intelligence)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return None
        try:
            data = json.loads(body.decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            ranking = result.get("ranking") or []
            notes = result.get("notes") or {}
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError, ValueError):
            return None
        out: list[DigestEntry] = []
        seen = set()
        for idx in ranking:
            try:
                index = int(idx)
            except (TypeError, ValueError):
                continue
            if index < 0 or index >= len(entries) or index in seen:
                continue
            seen.add(index)
            entry = entries[index]
            note = str(notes.get(str(index), ""))[:60] if isinstance(notes, dict) else ""
            out.append(replace(entry, note=note))
        return out


def render_brief_limited(
    plan: DigestPlan,
    date_label: str,
    per_block: int = _PER_BLOCK_DEFAULT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, TrimStats]:
    """渲染简报并保证字符上限；超限时从低重要度、同级别更旧者开始丢弃。"""
    text, rendered = _render_with_survivors(plan, date_label, per_block)
    kept = frozenset(e.id for e in rendered if e.id is not None)
    before = len(text)
    if len(text) <= max_chars or not plan.total:
        return text, TrimStats(
            trimmed=0,
            before_chars=before,
            after_chars=before,
            within_limit=len(text) <= max_chars,
            kept_ids=kept,
        )
    remaining = list(plan.total)
    dropped = 0
    while remaining:
        remaining, removed_now = _drop_worst(remaining)
        dropped += removed_now
        text, rendered = _render_with_survivors(
            DigestPlan(total=tuple(remaining), grouped=_group(remaining), skimmed=True),
            date_label,
            per_block,
        )
        kept = frozenset(e.id for e in rendered if e.id is not None)
        if len(text) <= max_chars:
            break
    return text, TrimStats(
        trimmed=dropped,
        before_chars=before,
        after_chars=len(text),
        within_limit=len(text) <= max_chars,
        kept_ids=kept,
    )


def render_brief(plan: DigestPlan, date_label: str, per_block: int = _PER_BLOCK_DEFAULT) -> str:
    text, _rendered = _render_with_survivors(plan, date_label, per_block)
    return text


def _render_with_survivors(
    plan: DigestPlan, date_label: str, per_block: int
) -> tuple[str, list[DigestEntry]]:
    """渲染并返回实际出现在正文中的条目集合（“同类另有 N 条”不算）。"""
    lines: list[str] = [f"📊 今日情报简报 {date_label}（近 3 日新内容）", ""]
    rendered: list[DigestEntry] = []
    top = [e for e in plan.total if e.importance in ("high", "medium")][:3]
    top_keys = {entry.url for entry in top}
    if top:
        lines.append("🔥 【今日重点】")
        for i, entry in enumerate(top, 1):
            rendered.append(entry)
            label = f"{i}. {entry.title}"
            note = entry.note or entry.summary[:60]
            lines.append(f"{label}｜{note}")
            lines.append(f"   📅 {_short_date(entry.published_at)} · 来源 {entry.publisher}")
            lines.append(f"   ↗ [原文链接]({entry.url})")
            for pub, link, date in entry.related_links:
                lines.append(f"   ↗ 另见：{pub} · [🔗 原文]({link})")
    lines.append("")
    for block_name, entries in plan.grouped.items():
        selected = [e for e in entries if e.url not in top_keys][:per_block]
        if not selected:
            continue
        more = sum(1 for e in entries if e.url not in top_keys) - len(selected)
        lines.append(f"📚 【{block_name}】")
        for entry in selected:
            rendered.append(entry)
            importance = IMPORTANCE_LABELS.get(entry.importance, "低")
            note = entry.note
            title_line = f"- {entry.title}（{importance}）"
            if note:
                title_line += f"｜{note}"
            lines.append(title_line)
            lines.append(f"   📅 {_short_date(entry.published_at)} · {entry.publisher} · [🔗原文]({entry.url})")
            for pub, link, _date in entry.related_links:
                lines.append(f"   ↗ 另见：{pub} · [🔗 原文]({link})")
        if more > 0:
            lines.append(f"  …同类另有 {more} 条剩阅")
    lines.append("")
    lines.append("— 每日情报自动汇总 · 已推内容次日不再重复 · 全部内容见控制台「今日情报」 —")
    return "\n".join(lines), rendered


def _is_demo(row: dict) -> bool:
    """演示/样式预览条目永久排除：绝不允许真实推送链路携带。

    （2026-08-27 用户反馈：样式验证时曾向测试群发送带
    “（样式预览）/示例摘要”标记的演示卡片，造成日期/链接误导。）
    """
    title = str(row.get("title") or "")
    summary = str(row.get("summary") or "")
    return any(marker in (title + " " + summary) for marker in DEMO_MARKERS)


def _drop_worst(entries: list[DigestEntry]) -> tuple[list[DigestEntry], int]:
    """丢弃最重要度最差者；同一重要度并列时更旧者先丢。

    返回（剩余条目, 本轮实际丢弃条数）：同键并列条目一轮全丢并按条目计数。
    """
    worst = max(IMPORTANCE_ORDER.get(e.importance, 2) for e in entries)
    oldest = min(
        (e.published_at or "")
        for e in entries
        if IMPORTANCE_ORDER.get(e.importance, 2) == worst
    )
    kept = [
        entry
        for entry in entries
        if not (
            IMPORTANCE_ORDER.get(entry.importance, 2) == worst
            and (entry.published_at or "") == oldest
        )
    ]
    return kept, len(entries) - len(kept)


def _short_date(published_at: str) -> str:
    raw = (published_at or "").strip()
    if not raw:
        return "日期未知"
    try:
        local = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            TIMEZONE
        )
        return local.date().isoformat()[5:]
    except ValueError:
        return raw[5:10]
