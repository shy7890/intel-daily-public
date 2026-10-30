from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from intel_daily.adapters import SourceItem
from intel_daily.db import connect, init_database


NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)
SH = ZoneInfo("Asia/Shanghai")


def item(external_id: str, title: str, **overrides) -> SourceItem:
    values = dict(
        external_id=external_id,
        title=title,
        publisher="国家能源局",
        published_at=datetime(2026, 8, 21, 17, 37, 4, tzinfo=SH),
        original_url=f"http://www.nea.gov.cn/{external_id}/c.html",
        summary="摘要",
        keywords=("储能",),
        fetched_at=NOW,
    )
    values.update(overrides)
    return SourceItem(**values)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "intel.db"
    init_database(path)
    return path


def test_upsert_is_idempotent_by_external_id(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    source_id = 1
    assert repository.upsert(source_id, item("a", "标题A")) == "inserted"
    assert repository.upsert(source_id, item("a", "标题A")) == "duplicate"
    assert repository.count() == 1


def test_item_rows_carry_source_metadata(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    repository.upsert(1, item("b", "标题B"))
    rows = repository.list_recent(10)
    row = rows[0]
    assert row["source_id"] == 1
    assert row["external_id"] == "b"
    assert row["original_url"].endswith("/b/c.html")
    assert row["canonical_url"].endswith("/b/c.html")
    assert row["publisher"] == "国家能源局"
    assert row["published_at"].startswith("2026-08-21")
    assert row["status"] == "collected"
    assert row["verification_status"] == "primary"
    assert row["summary"] == "摘要"


def test_same_fingerprint_marks_duplicate_of(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    original = item("c", "标题C")
    copy = item(
        "c-copy",
        "标题C",
        original_url="http://news.example.com/clickbait",
    )
    repository.upsert(1, original)
    assert repository.upsert(1, copy) == "duplicate"
    copy_row = repository.get_by_external_id(1, "c-copy")
    assert copy_row["duplicate_of_id"] == repository.get_by_external_id(1, "c")["id"]
    assert copy_row["status"] == "skipped"


def test_list_recent_orders_newest_first(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    repository.upsert(
        1,
        item("older", "旧文件", published_at=datetime(2026, 8, 20, 10, 0, tzinfo=SH)),
    )
    repository.upsert(
        1,
        item("newer", "新文件", published_at=datetime(2026, 8, 25, 10, 0, tzinfo=SH)),
    )
    rows = repository.list_recent(10)
    assert rows[0]["external_id"] == "newer"
    assert rows[1]["external_id"] == "older"


def test_canonical_url_drops_fragment_and_normalizes_host(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    repository.upsert(
        1,
        item(
            "d",
            "标题D",
            original_url="http://WWW.NEA.GOV.CN:80/d/c.html#section-2",
        ),
    )
    row = repository.get_by_external_id(1, "d")
    assert row["canonical_url"] == "http://www.nea.gov.cn/d/c.html"


def test_upsert_uses_source_type_from_sources_table(db_path: Path) -> None:
    """T11：source_type 不得硬编码 primary，须跟随 sources 表（二手源降级待核验）。"""
    from intel_daily.db import connect
    from intel_daily.items import ItemRepository

    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO sources (
                name, publisher, url, source_type, priority_tier,
                adapter_type, topic, region, enabled, license_risk,
                crawl_stability_risk
            ) VALUES ('某媒体', '某媒体', 'https://media.example.com/feed',
                      'secondary', 'P1', 'pending_html', '行业动态',
                      '中国大陆', 0, 'medium', 'medium')
            """
        )
        secondary_id = int(
            connection.execute(
                "SELECT id FROM sources WHERE url = 'https://media.example.com/feed'"
            ).fetchone()[0]
        )

    repository = ItemRepository(db_path)
    repository.upsert(secondary_id, item("m1", "媒体报道标题"))
    row = repository.get_by_external_id(secondary_id, "m1")
    assert row["source_type"] == "secondary"
    assert row["verification_status"] == "secondary_pending"


def test_upsert_accepts_explicit_secondary_source_type(db_path: Path) -> None:
    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    repository.upsert(1, item("m2", "转载标题"), source_type="secondary")
    row = repository.get_by_external_id(1, "m2")
    assert row["source_type"] == "secondary"
    assert row["verification_status"] == "secondary_pending"


def test_upsert_is_idempotent_when_external_id_reused_with_new_fingerprint(
    db_path: Path,
) -> None:
    """T1 兜底：同日重跑（cursor 用 >=）会以相同 external_id 再次入库，必须幂等。

    发布时间不同 → content_fingerprint 不同，但 UNIQUE(source_id, external_id)
    才是最终裁决：不得抛异常、不得产生第二条记录。
    """
    from datetime import timedelta

    from intel_daily.items import ItemRepository

    repository = ItemRepository(db_path)
    assert repository.upsert(1, item("r1", "同日重跑公告")) == "inserted"
    late = item(
        "r1",
        "同日重跑公告",
        published_at=datetime(2026, 8, 21, 17, 37, 4, tzinfo=SH) + timedelta(hours=6),
    )
    assert repository.upsert(1, late) == "duplicate"
    assert repository.count() == 1


def test_concurrent_upsert_of_same_external_id_never_raises(db_path: Path) -> None:
    """并发双写同一 external_id：由 UNIQUE 兜底，只落一条且不得抛 IntegrityError。"""
    import threading

    from intel_daily.items import ItemRepository

    outcomes: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            outcomes.append(ItemRepository(db_path).upsert(1, item("race", "并发公告")))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert outcomes.count("inserted") == 1
    assert set(outcomes) <= {"inserted", "duplicate"}
    assert ItemRepository(db_path).count() == 1
