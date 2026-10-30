import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 每日汇总 run 的 kind（worker 重入锁与“当日已跑”判定共用同一标识）。
DAILY_RUN_KIND = "每日汇总"

# running 行超过该时长视为进程崩溃遗留，可被抢占（抢占后记 failed 便于诊断）。
LOCK_STALE_HOURS = 2.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
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
    allow_external_ai INTEGER NOT NULL DEFAULT 0 CHECK (allow_external_ai IN (0, 1)),
    last_success_cursor TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    timezone TEXT NOT NULL,
    daily_time TEXT NOT NULL,
    weekdays_only INTEGER NOT NULL CHECK (weekdays_only IN (0, 1)),
    topics_json TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    exclude_words_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO settings (
    id,
    timezone,
    daily_time,
    weekdays_only,
    topics_json,
    keywords_json,
    exclude_words_json
) VALUES (
    1,
    'Asia/Shanghai',
    '09:00',
    0,
    '["宏观政策","私募监管","新能源政策","产业链","公司动态"]',
    '[]',
    '[]'
)
ON CONFLICT(id) DO NOTHING;

CREATE TABLE IF NOT EXISTS intelligence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    original_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    publisher TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'primary'
        CHECK (verification_status IN ('primary', 'secondary_pending', 'secondary_backfilled')),
    category TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_fingerprint TEXT NOT NULL,
    duplicate_of_id INTEGER REFERENCES intelligence_items(id),
    status TEXT NOT NULL DEFAULT 'collected'
        CHECK (status IN ('collected', 'selected', 'skipped', 'failed', 'sent')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_intelligence_items_fingerprint
    ON intelligence_items (content_fingerprint);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'daily',
    status TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed', 'running')),
    scheduled_for TEXT,
    started_at TEXT,
    finished_at TEXT,
    collected_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

SEED_SQL = """
INSERT INTO sources (
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
    crawl_stability_risk
) VALUES (
    :name,
    :publisher,
    :url,
    'primary',
    :priority_tier,
    :adapter_type,
    :topic,
    '中国大陆',
    0,
    'low',
    :crawl_stability_risk
)
ON CONFLICT(url) DO NOTHING
"""

PRIMARY_SOURCE_ROWS = (
    {
        "name": "中国政府网政策文件库",
        "publisher": "国务院",
        "url": "https://sousuo.www.gov.cn/zcwjk/policyDocumentLibrary?t=zhengcelibrary_gw",
        "topic": "宏观政策",
        "adapter_type": "pending_html",
        "crawl_stability_risk": "low-medium",
        "priority_tier": "P0",
    },
    {
        "name": "中国证监会规章库",
        "publisher": "中国证券监督管理委员会",
        "url": "https://www.csrc.gov.cn/csrc/c106256/fg.shtml",
        "topic": "私募监管",
        "adapter_type": "pending_html",
        "crawl_stability_risk": "low-medium",
        "priority_tier": "P0",
    },
    {
        "name": "国家能源局",
        "publisher": "国家能源局",
        "url": "https://www.nea.gov.cn/",
        "topic": "能源政策",
        "crawl_stability_risk": "low-medium",
        "adapter_type": "nea_json",
        "priority_tier": "P0",
    },
    {
        "name": "巨潮资讯公告(沪深北A股)",
        "publisher": "巨潮资讯网",
        "url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        "topic": "公司动态",
        "adapter_type": "cninfo_json",
        "crawl_stability_risk": "medium",
        "priority_tier": "P0",
    },
    {
        "name": "上期所每日行情(镍、锡)",
        "publisher": "上海期货交易所",
        "url": "https://www.shfe.com.cn/data/tradedata/future/dailydata/",
        "topic": "原材料价格",
        "adapter_type": "shfe_dat",
        "crawl_stability_risk": "low",
        "priority_tier": "P1",
    },
    {
        "name": "广期所碳酸锂期货行情",
        "publisher": "广州期货交易所",
        "url": "http://www.gfex.com.cn/gfex/sspzb/sspz.shtml",
        "topic": "原材料价格",
        "adapter_type": "gfex_json",
        "crawl_stability_risk": "medium",
        "priority_tier": "P1",
    },
    {
        "name": "国家发展改革委",
        "publisher": "国家发展和改革委员会",
        "url": "https://www.ndrc.gov.cn/xxgk/zcfb/",
        "topic": "产业政策",
        "adapter_type": "pending_html",
        "crawl_stability_risk": "low-medium",
        "priority_tier": "P0",
    },
    {
        "name": "工业和信息化部",
        "publisher": "工业和信息化部",
        "url": "https://www.miit.gov.cn/gyhxxhbwjcx/",
        "topic": "产业运行",
        "adapter_type": "pending_html",
        "crawl_stability_risk": "medium",
        "priority_tier": "P0",
    },
    {
        "name": "中汽协行业统计 CAAM(tjsj)",
        "publisher": "中国汽车工业协会",
        "url": "http://www.caam.org.cn/tjsj",
        "topic": "行业动态",
        "adapter_type": "caam_html",
        "crawl_stability_risk": "low-medium",
        "priority_tier": "P0",
    },
    {
        "name": "乘联会新能源月度报告 CPCA(bgzl)",
        "publisher": "中国汽车流通协会乘用车市场信息联席分会（CPCA）",
        "url": "https://www.cpcaauto.com/news.php?types=bgzl&anid=126",
        "topic": "行业动态",
        "adapter_type": "cpca_html",
        "crawl_stability_risk": "low-medium",
        "priority_tier": "P0",
    },
    {
        "name": "UN Comtrade 锂电池进出口(HS 850760)",
        "publisher": "UN Comtrade",
        "url": "https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=156&cmdCode=850760&partnerCode=0&flowCode=X",
        "topic": "行业动态",
        "adapter_type": "comtrade_json",
        "crawl_stability_risk": "low",
        "priority_tier": "P0",
    },
)


# D-008：新增信源默认关闭外部 AI（allow_external_ai=0），不随 P0 批量规则置 1，
# 必须由管理员显式开启。
_OPT_OUT_EXTERNAL_AI_URLS = (
    "http://www.caam.org.cn/tjsj",
    "https://www.cpcaauto.com/news.php?types=bgzl&anid=126",
    "https://comtradeapi.un.org/public/v1/preview/C/A/HS?reporterCode=156&cmdCode=850760&partnerCode=0&flowCode=X",
)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


_SEED_URLS_ALLOWING_AI = tuple(
    row["url"]
    for row in PRIMARY_SOURCE_ROWS
    if row.get("priority_tier") == "P0"
    and row["url"] not in _OPT_OUT_EXTERNAL_AI_URLS
)


def _migrate_sources(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(sources)")
    }
    if "allow_external_ai" not in columns:
        connection.execute(
            "ALTER TABLE sources ADD COLUMN allow_external_ai "
            "INTEGER NOT NULL DEFAULT 0 CHECK (allow_external_ai IN (0, 1))"
        )
    placeholders = ",".join("?" for _ in _SEED_URLS_ALLOWING_AI)
    connection.execute(
        f"""
        UPDATE sources
        SET allow_external_ai = 1
        WHERE source_type = 'primary'
          AND priority_tier = 'P0'
          AND url IN ({placeholders})
        """,
        _SEED_URLS_ALLOWING_AI,
    )
    connection.execute(
        """
        UPDATE sources
        SET adapter_type = 'nea_json'
        WHERE name = '国家能源局' AND adapter_type = 'pending_html'
        """
    )


def init_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.executescript(SCHEMA_SQL)
        _migrate_sources(connection)
        connection.executemany(SEED_SQL, PRIMARY_SOURCE_ROWS)


def _parse_ts(value: object) -> datetime | None:
    """解析 runs 表时间戳（ISO，兼容尾部 Z）；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def acquire_daily_lock(
    path: Path,
    run_date: str,
    started_at: str,
    kind: str = DAILY_RUN_KIND,
    stale_hours: float = LOCK_STALE_HOURS,
) -> int | None:
    """获取当日汇总重入锁：runs 表中 kind=? 且 status='running' 的行。

    - 无 running 行 → 插入 running 行并返回其 id（开 run 即写 started_at）；
    - 存在未超时 running 行 → 返回 None，调用方须跳过本轮（防并发双开/双发）；
    - 存在超时陈旧行 → 先记 failed（抢占）再插入新行，进程崩溃后可自愈。

    “检查 + 插入”在 BEGIN IMMEDIATE 事务内完成，保证多进程互斥。
    """
    now = datetime.now(timezone.utc)
    connection = connect(path)
    try:
        # 手动管理事务：connect() 默认模式下隐式事务会与本语句冲突。
        connection.isolation_level = None
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                """
                SELECT id, started_at
                FROM runs
                WHERE kind = ? AND status = 'running'
                ORDER BY id
                """,
                (kind,),
            ).fetchall()
            for row in rows:
                started = _parse_ts(row["started_at"])
                fresh = (
                    started is not None
                    and (now - started).total_seconds() <= stale_hours * 3600
                )
                if fresh:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'failed',
                        finished_at = ?,
                        error_summary = ?
                    WHERE id = ?
                    """,
                    (
                        now.isoformat(),
                        f"陈旧 running 锁被抢占（超过 {stale_hours:g} 小时未完成）",
                        row["id"],
                    ),
                )
            inserted = connection.execute(
                """
                INSERT INTO runs (
                    kind,
                    status,
                    scheduled_for,
                    started_at,
                    finished_at,
                    collected_count,
                    selected_count,
                    sent_count,
                    failed_count,
                    error_summary,
                    created_at
                ) VALUES (?, 'running', ?, ?, NULL, 0, 0, 0, 0, '',
                          strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (kind, run_date, started_at),
            )
            run_id = int(inserted.lastrowid)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        return run_id
    finally:
        connection.close()


def release_daily_lock(path: Path, run_id: int) -> None:
    """释放未产生任何执行的重入锁（删除 running 行）。

    当日无事可做（无启用信源）时不能留下 running 行，否则后续重试会被误判为“已在运行”。
    """
    with connect(path) as connection:
        connection.execute(
            "DELETE FROM runs WHERE id = ? AND status = 'running'",
            (run_id,),
        )
