from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SourceItem:
    external_id: str
    title: str
    publisher: str
    published_at: datetime
    original_url: str
    summary: str
    keywords: tuple[str, ...]
    fetched_at: datetime


@dataclass(frozen=True)
class FetchBatch:
    items: list[SourceItem]
    new_cursor: str
