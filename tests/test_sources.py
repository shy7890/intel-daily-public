from pathlib import Path

from fastapi.testclient import TestClient


def settings_for(tmp_path: Path):
    from intel_daily.config import AppSettings

    return AppSettings(
        db_path=tmp_path / "intel.db",
        admin_username="admin",
        admin_password="correct-horse",
    )


def test_database_seeds_primary_sources_once(tmp_path: Path) -> None:
    from intel_daily.db import init_database
    from intel_daily.sources import SourceRepository

    db_path = tmp_path / "intel.db"

    init_database(db_path)
    init_database(db_path)
    sources = SourceRepository(db_path).list_all()

    assert [source.name for source in sources] == [
        "中国政府网政策文件库",
        "中国证监会规章库",
        "国家能源局",
        "巨潮资讯公告(沪深北A股)",
        "上期所每日行情(镍、锡)",
        "广期所碳酸锂期货行情",
        "国家发展改革委",
        "工业和信息化部",
        "中汽协行业统计 CAAM(tjsj)",
        "乘联会新能源月度报告 CPCA(bgzl)",
        "UN Comtrade 锂电池进出口(HS 850760)",
    ]
    assert all(source.source_type == "primary" for source in sources)
    assert all(source.priority_tier in ("P0", "P1") for source in sources)
    assert all(
        source.priority_tier == "P1"
        for source in sources
        if source.topic == "原材料价格"
    )
    assert all(source.enabled is False for source in sources)


def test_sources_page_shows_authority_metadata(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    response = TestClient(app).get(
        "/sources",
        auth=("admin", "correct-horse"),
    )

    assert response.status_code == 200
    assert "中国政府网政策文件库" in response.text
    assert "一手" in response.text
    assert "P0" in response.text
    assert "尚未接入抓取" in response.text


def test_admin_can_disable_a_source(tmp_path: Path) -> None:
    from intel_daily.sources import SourceRepository
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    repository = SourceRepository(tmp_path / "intel.db")
    source_id = repository.list_all()[0].id
    repository.set_enabled(source_id, True)

    response = TestClient(app).post(
        f"/sources/{source_id}/enabled",
        data={"enabled": "false"},
        auth=("admin", "correct-horse"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/sources"
    assert repository.get(source_id).enabled is False


def test_source_toggle_rejects_unknown_source(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    response = TestClient(app, raise_server_exceptions=False).post(
        "/sources/99999/enabled",
        data={"enabled": "true"},
        auth=("admin", "correct-horse"),
    )

    assert response.status_code == 404


def test_source_toggle_rejects_ambiguous_boolean(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    response = TestClient(app, raise_server_exceptions=False).post(
        "/sources/1/enabled",
        data={"enabled": "yes"},
        auth=("admin", "correct-horse"),
    )

    assert response.status_code == 422


def test_database_migrates_external_ai_flag(tmp_path: Path) -> None:
    from intel_daily.db import init_database
    from intel_daily.sources import SourceRepository

    db_path = tmp_path / "intel.db"
    init_database(db_path)
    init_database(db_path)

    sources = SourceRepository(db_path).list_all()
    assert len(sources) == 11
    p0 = next(s for s in sources if s.name == "国家能源局")
    assert p0.allow_external_ai is True
    cninfo = next(s for s in sources if s.name == "巨潮资讯公告(沪深北A股)")
    assert cninfo.adapter_type == "cninfo_json"
    assert cninfo.priority_tier == "P0"
    assert cninfo.topic == "公司动态"
    assert cninfo.allow_external_ai is True
    shfe = next(s for s in sources if s.name == "上期所每日行情(镍、锡)")
    assert shfe.adapter_type == "shfe_dat"
    assert shfe.priority_tier == "P1"
    assert shfe.topic == "原材料价格"
    assert shfe.allow_external_ai is False
    gfex = next(s for s in sources if s.name == "广期所碳酸锂期货行情")
    assert gfex.adapter_type == "gfex_json"
    assert gfex.priority_tier == "P1"
    assert gfex.topic == "原材料价格"
    assert gfex.allow_external_ai is False
    caam = next(s for s in sources if s.name == "中汽协行业统计 CAAM(tjsj)")
    assert caam.adapter_type == "caam_html"
    assert caam.priority_tier == "P0"
    assert caam.topic == "行业动态"
    assert caam.enabled is False
    assert caam.allow_external_ai is False
    cpca = next(s for s in sources if s.name == "乘联会新能源月度报告 CPCA(bgzl)")
    assert cpca.adapter_type == "cpca_html"
    assert cpca.priority_tier == "P0"
    assert cpca.enabled is False
    assert cpca.allow_external_ai is False
    comtrade = next(s for s in sources if s.name == "UN Comtrade 锂电池进出口(HS 850760)")
    assert comtrade.adapter_type == "comtrade_json"
    assert comtrade.priority_tier == "P0"
    assert comtrade.enabled is False
    assert comtrade.allow_external_ai is False


def test_database_upgrades_legacy_sources_table(tmp_path: Path) -> None:
    import sqlite3

    from intel_daily.db import init_database
    from intel_daily.sources import SourceRepository

    legacy_ddl = """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        publisher TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL CHECK (source_type IN ('primary', 'secondary')),
        priority_tier TEXT NOT NULL CHECK (priority_tier IN ('P0', 'P1')),
        adapter_type TEXT NOT NULL,
        topic TEXT NOT NULL,
        region TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
        license_risk TEXT NOT NULL,
        crawl_stability_risk TEXT NOT NULL,
        last_success_cursor TEXT,
        last_success_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(legacy_ddl)
        connection.execute(
            "INSERT INTO sources (name, publisher, url, source_type, priority_tier, adapter_type, topic, region, enabled, license_risk, crawl_stability_risk) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            ("旧信源", "旧发布者", "https://legacy.example.com/", "primary", "P0", "legacy", "产业政策", "中国大陆", "low", "low"),
        )
    init_database(db_path)

    repository = SourceRepository(db_path)
    legacy_source = repository.list_all()[0]
    assert legacy_source.name == "旧信源"
    assert legacy_source.allow_external_ai is False
    assert repository.set_allow_external_ai(legacy_source.id, True)
    assert repository.get(legacy_source.id).allow_external_ai is True


def test_admin_can_toggle_external_ai_per_source(tmp_path: Path) -> None:
    from intel_daily.sources import SourceRepository
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    repository = SourceRepository(tmp_path / "intel.db")
    source_id = repository.list_all()[0].id

    response = TestClient(app).post(
        f"/sources/{source_id}/ai",
        data={"allow": "false"},
        auth=("admin", "correct-horse"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repository.get(source_id).allow_external_ai is False

    invalid = TestClient(app, raise_server_exceptions=False).post(
        f"/sources/{source_id}/ai",
        data={"allow": "yes"},
        auth=("admin", "correct-horse"),
    )
    assert invalid.status_code == 422

    missing = TestClient(app, raise_server_exceptions=False).post(
        "/sources/99999/ai",
        data={"allow": "true"},
        auth=("admin", "correct-horse"),
    )
    assert missing.status_code == 404
