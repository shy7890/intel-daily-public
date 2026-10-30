import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import sqlite3
from pathlib import Path

_TIMEZONE = ZoneInfo("Asia/Shanghai")

SESSION_LABELS = {"high": "高", "medium": "中", "low": "低"}
TRACKED_UNKNOWN = "未知"

_DEFAULT_TIMEOUT = 15

FETCH_LIMIT = 100


def _window_floor(now: date | datetime | None, days: int) -> str:
    """窗口下界（YYYY-MM-DD）：now 缺省取真实当前时间（UTC，与原 date('now') 一致）。"""
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
            tzinfo=timezone.utc
        )
    else:
        current = datetime.now(timezone.utc).replace(
            year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
        )
    return (current.date() - timedelta(days=days)).isoformat()


def count_window_items(
    db_path: Path, days: int = 3, now: date | datetime | None = None
) -> int:
    """窗口内 collected/selected 条目总数（不受 FETCH_LIMIT 截断，用于诊断）。"""
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM intelligence_items
            WHERE status IN ('collected', 'selected')
              AND substr(published_at, 1, 10) >= ?
            """,
            (_window_floor(now, days),),
        ).fetchone()
    finally:
        connection.close()
    return int(row[0])


def fetch_items(
    db_path: Path,
    days: int = 3,
    include_sent: bool = False,
    now: date | datetime | None = None,
) -> list[dict]:
    """读取最近窗口内未推送过（collected/selected）的情报条目。

    `days` 为发布时间的最近天数窗口；已推送（status='sent'）默认永不重复，
    `include_sent=True` 时该窗口内仍包含已推送条目（临时测试发送重复使用）。
    `now` 可注入基准日期（date/datetime），缺省使用真实当前时间，便于测试免时钟漂移。
    """
    status_filter = (
        "'collected', 'selected', 'sent'" if include_sent else "'collected', 'selected'"
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT
                id, external_id, title, original_url, publisher, published_at,
                category, importance, summary, verification_status
            FROM intelligence_items
            WHERE status IN ({status_filter})
              AND substr(published_at, 1, 10) >= ?
            ORDER BY published_at DESC, id DESC
            LIMIT {FETCH_LIMIT}
            """,
            (_window_floor(now, days),),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def build_digest(db_path: Path, date_label: str) -> str:
    """从情报库最近条目生成飞书文本摘要（按类别分组）。

    只包含有原文链接的条目；待核验条目显式标记。
    """
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                title, original_url, publisher, published_at,
                category, importance, summary, verification_status
            FROM intelligence_items
            WHERE status IN ('collected', 'selected')
            ORDER BY published_at DESC, id DESC
            LIMIT 100
            """
        ).fetchall()
    finally:
        connection.close()
    return render_digest([dict(row) for row in rows], date_label=date_label)

@dataclass(frozen=True)
class DeliveryReceipt:
    ok: bool
    error: str | None = None
    dry_run: bool = False


class Notifier(Protocol):
    def send(self, text: str) -> DeliveryReceipt: ...


class StubNotifier:
    """默认通知器：仅记录，不发送任何消息。"""

    def send(self, text: str) -> DeliveryReceipt:
        return DeliveryReceipt(ok=True, dry_run=True)


class FeishuWebhookNotifier:
    """飞书自定义群机器人 webhook 发送器（官方 bot/v2/hook 协议）。"""

    def __init__(
        self,
        webhook_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, text: str) -> DeliveryReceipt:
        return self.send_payload({"msg_type": "text", "content": {"text": text}})

    def send_card(self, card: dict) -> DeliveryReceipt:
        return self.send_payload(card)

    def send_payload(self, payload: dict) -> DeliveryReceipt:
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "intel-daily/0.1 (+internal daily intelligence)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            return DeliveryReceipt(ok=False, error=f"飞书接口返回 {exc.code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            return DeliveryReceipt(ok=False, error=f"飞书接口不可达: {exc}")
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return DeliveryReceipt(ok=False, error=f"飞书响应解析失败: {exc}")
        code = data.get("code", -1)
        if code != 0:
            return DeliveryReceipt(
                ok=False, error=f"飞书业务失败 code={code} msg={data.get('msg', '')}"
            )
        return DeliveryReceipt(ok=True)


def render_digest(rows: list[dict], date_label: str) -> str:
    usable = [row for row in rows if row.get("original_url")]
    skipped = len(rows) - len(usable)
    lines: list[str] = [f"新能源每日情报 {date_label}"]
    by_category: dict[str, list[dict]] = {}
    for row in usable:
        by_category.setdefault(row.get("category") or TRACKED_UNKNOWN, []).append(row)
    for category in sorted(by_category):
        lines.append(f"\n【{category}】")
        for row in by_category[category]:
            importance = SESSION_LABELS.get(row.get("importance"), TRACKED_UNKNOWN)
            publisher = row.get("publisher") or ""
            published_on = _local_date(row.get("published_at"))
            lines.append(f"- {row.get('title', '').strip()}")
            lines.append(f"  {row.get('summary', '').strip()}")
            flags = []
            if row.get("verification_status") == "secondary_pending":
                flags.append("待核验")
            if flags:
                lines.append(f"  ({'、'.join(flags)})")
            lines.append(f"  [重要程度 {importance}｜来源 {publisher}｜发布时间 {published_on}]")
            lines.append(f"  原链 {row.get('original_url', '')}")
    if skipped:
        lines.append(f"\n({skipped} 条无有效原文链接，已跳过)")
    return "\n".join(lines)


def _local_date(published_at: str) -> str:
    raw = (published_at or "").strip()
    if not raw:
        return "日期未知"
    try:
        return (
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            .astimezone(_TIMEZONE)
            .date()
            .isoformat()
        )
    except ValueError:
        return raw[:10]


def render_card(markdown_text: str, date_label: str) -> dict:
    """把简报文本封装为飞书交互卡片（header 蓝色 + 图标化标题）。"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 今日新能源情报 {date_label}",
                },
            },
            "elements": [
                {"tag": "markdown", "content": markdown_text},
            ],
        },
    }
