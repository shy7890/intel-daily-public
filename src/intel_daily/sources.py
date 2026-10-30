from dataclasses import dataclass
from pathlib import Path

from intel_daily.db import connect


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    publisher: str
    url: str
    source_type: str
    priority_tier: str
    adapter_type: str
    topic: str
    region: str
    enabled: bool
    license_risk: str
    crawl_stability_risk: str
    last_success_cursor: str | None
    last_success_at: str | None
    allow_external_ai: bool = False


class SourceRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def list_all(self) -> list[Source]:
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    name,
                    publisher,
                    url,
                    source_type,
                    priority_tier,
                    adapter_type,
                    topic,
                    region,
                    enabled,
                    license_risk,
                    crawl_stability_risk,
                    allow_external_ai,
                    last_success_cursor,
                    last_success_at
                FROM sources
                ORDER BY id
                """
            ).fetchall()
        return [_source_from_row(row) for row in rows]

    def get(self, source_id: int) -> Source:
        with connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    name,
                    publisher,
                    url,
                    source_type,
                    priority_tier,
                    adapter_type,
                    topic,
                    region,
                    enabled,
                    license_risk,
                    crawl_stability_risk,
                    allow_external_ai,
                    last_success_cursor,
                    last_success_at
                FROM sources
                WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            raise SourceNotFoundError(source_id)
        return _source_from_row(row)

    def advance_success(
        self, source_id: int, cursor: str, success_at: str
    ) -> Source:
        with connect(self.db_path) as connection:
            result = connection.execute(
                """
                UPDATE sources
                SET
                    last_success_cursor = ?,
                    last_success_at = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (cursor, success_at, source_id),
            )
        if result.rowcount == 0:
            raise SourceNotFoundError(source_id)
        return self.get(source_id)

    def set_allow_external_ai(self, source_id: int, allowed: bool) -> Source:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE sources
                SET
                    allow_external_ai = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (int(allowed), source_id),
            )
        if cursor.rowcount == 0:
            raise SourceNotFoundError(source_id)
        return self.get(source_id)

    def set_enabled(self, source_id: int, enabled: bool) -> Source:
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE sources
                SET
                    enabled = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (int(enabled), source_id),
            )
        if cursor.rowcount == 0:
            raise SourceNotFoundError(source_id)
        return self.get(source_id)


class SourceNotFoundError(KeyError):
    def __init__(self, source_id: int) -> None:
        super().__init__(f"source {source_id} does not exist")
        self.source_id = source_id


def _source_from_row(row) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        publisher=row["publisher"],
        url=row["url"],
        source_type=row["source_type"],
        priority_tier=row["priority_tier"],
        adapter_type=row["adapter_type"],
        topic=row["topic"],
        region=row["region"],
        enabled=bool(row["enabled"]),
        license_risk=row["license_risk"],
        crawl_stability_risk=row["crawl_stability_risk"],
        allow_external_ai=bool(row["allow_external_ai"]),
        last_success_cursor=row["last_success_cursor"],
        last_success_at=row["last_success_at"],
    )
