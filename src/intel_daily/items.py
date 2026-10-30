import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from intel_daily.adapters import SourceItem
from intel_daily.db import connect

_WS_RE = re.compile(r"\s+")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def content_fingerprint(item: SourceItem) -> str:
    title_norm = _WS_RE.sub(" ", item.title).strip()
    published_utc = item.published_at.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha256(f"{title_norm}|{published_utc}".encode("utf-8"))
    return digest.hexdigest()


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _source_type_of(connection: sqlite3.Connection, source_id: int) -> str:
    """未显式传入时按 source_id 回查 sources 表；信源不存在时保守按 primary 处理。"""
    row = connection.execute(
        "SELECT source_type FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    return str(row["source_type"]) if row is not None else "primary"


class ItemRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def upsert(
        self,
        source_id: int,
        item: SourceItem,
        category: str = "",
        importance: str = "",
        source_type: str | None = None,
    ) -> str:
        """按 UNIQUE(source_id, external_id) 幂等入库，重复拉取（如同日重跑）不会报错。

        `source_type` 由调用方从 sources 表传入（不再硬编码 primary）；
        未传时回退查 sources 表，二手源的核验状态一律 secondary_pending。
        """
        fingerprint = content_fingerprint(item)
        with connect(self.db_path) as connection:
            resolved_type = source_type or _source_type_of(connection, source_id)
            verification_status = (
                "primary" if resolved_type == "primary" else "secondary_pending"
            )
            existing = connection.execute(
                """
                SELECT id FROM intelligence_items
                WHERE source_id = ? AND external_id = ?
                """,
                (source_id, item.external_id),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            duplicate_of = connection.execute(
                """
                SELECT id FROM intelligence_items
                WHERE content_fingerprint = ?
                ORDER BY id
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            try:
                connection.execute(
                    """
                    INSERT INTO intelligence_items (
                        source_id,
                        external_id,
                        title,
                        original_url,
                        canonical_url,
                        publisher,
                        published_at,
                        fetched_at,
                        source_type,
                        verification_status,
                        category,
                        importance,
                        summary,
                        content_fingerprint,
                        duplicate_of_id,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        item.external_id,
                        item.title,
                        item.original_url,
                        canonicalize_url(item.original_url),
                        item.publisher,
                        _to_utc_iso(item.published_at),
                        _to_utc_iso(item.fetched_at),
                        resolved_type,
                        verification_status,
                        category,
                        importance,
                        item.summary,
                        fingerprint,
                        duplicate_of["id"] if duplicate_of is not None else None,
                        "skipped" if duplicate_of is not None else "collected",
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发写入或同日重跑撞 UNIQUE(source_id, external_id)：视为已入库。
                return "duplicate"
        return "duplicate" if duplicate_of is not None else "inserted"

    def update_summary(self, item_id: int, summary: str) -> None:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE intelligence_items
                SET summary = ?
                WHERE id = ?
                """,
                (summary, item_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"intelligence item {item_id} does not exist")

    def set_verification_status(self, item_id: int, status: str) -> None:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE intelligence_items
                SET verification_status = ?
                WHERE id = ?
                """,
                (status, item_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"intelligence item {item_id} does not exist")

    def mark_sent(self, item_ids: list[int]) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        with connect(self.db_path) as connection:
            connection.execute(
                f"""
                UPDATE intelligence_items
                SET status = 'sent'
                WHERE id IN ({placeholders})
                  AND status IN ('collected', 'selected')
                """,
                item_ids,
            )

    def count(self) -> int:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM intelligence_items"
            ).fetchone()
        return int(row[0])

    def list_recent(self, limit: int) -> list[sqlite3.Row]:
        with connect(self.db_path) as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    source_id,
                    external_id,
                    title,
                    original_url,
                    canonical_url,
                    publisher,
                    published_at,
                    fetched_at,
                    source_type,
                    verification_status,
                    category,
                    importance,
                    summary,
                    content_fingerprint,
                    duplicate_of_id,
                    status
                FROM intelligence_items
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get(self, item_id: int) -> sqlite3.Row:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    source_id,
                    external_id,
                    title,
                    original_url,
                    canonical_url,
                    publisher,
                    published_at,
                    fetched_at,
                    source_type,
                    verification_status,
                    category,
                    importance,
                    summary,
                    content_fingerprint,
                    duplicate_of_id,
                    status
                FROM intelligence_items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"intelligence item {item_id} does not exist")
        return row

    def get_by_external_id(
        self, source_id: int, external_id: str
    ) -> sqlite3.Row:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    source_id,
                    external_id,
                    title,
                    original_url,
                    canonical_url,
                    publisher,
                    published_at,
                    fetched_at,
                    source_type,
                    verification_status,
                    category,
                    importance,
                    summary,
                    content_fingerprint,
                    duplicate_of_id,
                    status
                FROM intelligence_items
                WHERE source_id = ? AND external_id = ?
                """,
                (source_id, external_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"intelligence item {external_id} does not exist")
        return row
