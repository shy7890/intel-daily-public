import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DigestEntry = Any

_WS_RE = re.compile(r"\s+")
_KEEP_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_title(title: str) -> str:
    """标题规范化：仅保留字母/数字/中文，转为小写，用于确定性同事件判定。"""
    kept = _KEEP_RE.sub("", title).lower()
    return _WS_RE.sub("", kept)


@dataclass(frozen=True)
class MergeResult:
    entries: tuple[DigestEntry, ...]
    removed: int
    # 被合并（替身）条目的 id：发送成功后必须与 canonical 一并 mark_sent，
    # 否则次日替身条目会单独再推一次。
    absorbed_ids: tuple[int, ...] = ()
    # (canonical_id, absorbed_id) 归属对：canonical 被裁剪未存活时，
    # 其替身也不应被标记已发送。
    absorbed_of: tuple[tuple[int, int], ...] = ()


def merge_similar(
    entries: list[DigestEntry], ai: Any = None, max_ai_entries: int = 60
) -> MergeResult:
    """同事件合并：先确定性命中（规范化标题完全一致），再交给 AI 按相似度分组。

    合并语义：组内保留最早发布（时间相同取先到）的一条为canonical，
    其余条的 来源/原文链接/发布时间 记录到 canonical.related_links 后被移除。
    ai 分组失败（返回 None）时静默回退确定性结果；不改变其他条目。
    """
    current = list(entries)
    removed = 0
    absorbed: list[tuple[int, int]] = []

    deterministic: list[list[int]] = []
    by_title: dict[str, list[int]] = {}
    for i, entry in enumerate(current):
        by_title.setdefault(normalize_title(entry.title), []).append(i)
    deterministic = [indices for indices in by_title.values() if len(indices) > 1]

    if deterministic:
        current, dropped, pairs = _apply_groups(current, deterministic)
        removed += dropped
        absorbed.extend(pairs)

    if ai is not None and len(current) <= max_ai_entries:
        groups = ai.find_groups(current)
        if groups:
            current, dropped, pairs = _apply_groups(current, groups)
            removed += dropped
            absorbed.extend(pairs)

    return MergeResult(
        entries=tuple(current),
        removed=removed,
        absorbed_ids=tuple(a for _c, a in absorbed),
        absorbed_of=tuple(absorbed),
    )


def _apply_groups(
    entries: list[DigestEntry], groups: list[list[int]]
) -> tuple[list[DigestEntry], int, list[tuple[int, int]]]:
    keep = list(entries)
    dropped = 0
    pairs: list[tuple[int, int]] = []
    occupied: set[int] = set()
    for group in groups:
        valid = [i for i in group if i < len(entries) and i not in occupied]
        if len(valid) < 2:
            continue
        canonical_idx = min(
            valid,
            key=lambda i: (entries[i].published_at or "", i),
        )
        others = [i for i in valid if i != canonical_idx]
        related = sorted(
            (
                (entries[i].publisher, entries[i].url, entries[i].published_at)
                for i in others
            ),
            key=lambda item: item[2] or "",
        )
        keep[canonical_idx] = _merge_into(entries[canonical_idx], related)
        canonical_id = entries[canonical_idx].id
        for i in others:
            absorbed_id = entries[i].id
            if canonical_id is not None and absorbed_id is not None:
                pairs.append((canonical_id, absorbed_id))
            occupied.add(i)
            keep[i] = None  # type: ignore[assignment]
        dropped += len(others)
        occupied.add(canonical_idx)
    out = [e for e in keep if e is not None]
    return out, dropped, pairs


def _merge_into(
    canonical: DigestEntry, related: list[tuple[str, str, str]]
) -> DigestEntry:
    from dataclasses import replace

    return replace(canonical, related_links=canonical.related_links + tuple(related))


class SimilarityMerger:
    """AI 相似合并器契约：返回“同事件”分组的索引列表（组内 ≥2 条），失败返回 None。"""

    def find_groups(self, entries: list[DigestEntry]) -> list[list[int]] | None:
        raise NotImplementedError


_SIMILARITY_SYSTEM_PROMPT = (
    "你是公司新能源投资情报去重助手。下列条目（编号即索引）来自不同信源。"
    "找出语义上属于“同一事件/同一发布内容”的条目（同一公告/同一政策/同一行情/"
    "同一新闻被不同来源转发），输出它们的索引分组。规则：不同事件不分组；"
    "单条不出现的索引不要列出；每组至少 2 条；只输出 JSON（不要任何其他文字）："
    '{"groups": [[索引, 索引], ...]}'
)


class DeepSeekSimilarityMerger(SimilarityMerger):
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

    def find_groups(self, entries: list[DigestEntry]) -> list[list[int]] | None:
        if not entries:
            return []
        items = [
            {"i": i, "title": e.title[:80], "summary": e.summary[:120], "published_at": e.published_at[:10]}
            for i, e in enumerate(entries)
        ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SIMILARITY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "temperature": 0.1,
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
            result = json.loads(data["choices"][0]["message"]["content"])
            return self.parse_groups(result, entries)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError, ValueError):
            return None

    def parse_groups(
        self, result: dict, entries: list[DigestEntry]
    ) -> list[list[int]]:
        groups: list[list[int]] = []
        seen: set[int] = set()
        raw = result.get("groups") or []
        if not isinstance(raw, list):
            return groups
        for group in raw:
            if not isinstance(group, list):
                continue
            indices: list[int] = []
            for idx in group:
                try:
                    index = int(idx)
                except (TypeError, ValueError):
                    continue
                if index < 0 or index >= len(entries) or index in seen:
                    continue
                indices.append(index)
            if len(indices) < 2:
                continue
            groups.append(indices)
            seen.update(indices)
        return groups
