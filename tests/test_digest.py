from datetime import datetime, timezone

import pytest


NOW = datetime(2026, 8, 26, 6, 0, 0, tzinfo=timezone.utc)


def _row(external_id: str, title: str, category: str = "储能", importance: str = "medium", **overrides) -> dict:
    values = dict(
        id=len(external_id),
        external_id=external_id,
        title=title,
        category=category,
        importance=importance,
        summary=f"{title}的摘要内容。",
        publisher="国家能源局",
        published_at="2026-08-21T09:00:00+00:00",
        original_url=f"http://www.nea.gov.cn/20260826/{external_id}/c.html",
        verification_status="primary",
        source_id=3,
    )
    values.update(overrides)
    return values


def test_block_mapping_keeps_few_blocks() -> None:
    from intel_daily.digest import block_of, BLOCKS

    assert block_of("私募监管") == "政策与监管"
    assert block_of("储能") == "新能源与储能"
    assert block_of("电力市场") == "新能源与储能"
    assert block_of("产业链") == "材料与产业链"
    assert block_of("公司动态") == "公司动态"
    assert block_of("行业动态") == "行业动态"
    names = [b[0] for b in BLOCKS]
    assert len(names) <= 6


def test_rule_fallback_ranks_importance_and_caps_total() -> None:
    from intel_daily.digest import plan_entries

    rows = [
        _row(f"r{i}", f"标题{i}", importance=("high" if i < 3 else "low"))
        for i in range(40)
    ]
    plan = plan_entries(rows, ai=None, max_total=12)

    assert len(plan.total) == 12
    assert plan.total[0].importance == "high"
    assert plan.total[1].importance == "high"
    assert plan.total[0].title == "标题0"


def test_rule_fallback_same_level_newest_first() -> None:
    # AI 缺失/失败兜底排序：同一重要度内“最新优先”，不得让最旧条目霸占名额
    from intel_daily.digest import plan_entries

    rows = [
        _row("old", "旧闻", importance="medium", published_at="2026-08-20T09:00:00+00:00"),
        _row("mid", "中闻", importance="medium", published_at="2026-08-22T09:00:00+00:00"),
        _row("new", "新闻", importance="medium", published_at="2026-08-24T09:00:00+00:00"),
    ]
    plan = plan_entries(rows, ai=None)
    assert [e.title for e in plan.total] == ["新闻", "中闻", "旧闻"]


def test_plan_skips_items_without_url() -> None:
    from intel_daily.digest import plan_entries

    rows = [_row("no-url", "无链条目", original_url="")]
    plan = plan_entries(rows, ai=None)
    assert plan.total == ()


def test_render_brief_has_blocks_and_top_summary() -> None:
    from intel_daily.digest import plan_entries, render_brief

    rows = [
        _row("a", "国家重点储能政策落地", category="储能", importance="high"),
        _row("b", "光伏装机数据发布", category="新能源", importance="medium"),
        _row("c", "碳酸锂价格波动", category="产业链", importance="low"),
    ]
    plan = plan_entries(rows, ai=None)
    text = render_brief(plan, date_label="2026-08-26")

    assert "今日情报简报 2026-08-26" in text
    assert "材料与产业链" in text
    assert "国家重点储能政策落地" in text
    assert "碳酸锂价格波动" in text
    assert "http://www.nea.gov.cn/20260826/a/c.html" in text


def test_render_brief_is_compact_for_many_rows() -> None:
    from intel_daily.digest import plan_entries, render_brief

    rows = [_row(f"x{i}", f"标题条目{i}", category="储能", importance="medium") for i in range(60)]
    plan = plan_entries(rows, ai=None)
    text = render_brief(plan, date_label="2026-08-26")

    assert len(text) < 6000
    assert text.count("http://") <= 12


def test_top_items_not_repeated_in_blocks() -> None:
    from intel_daily.digest import plan_entries, render_brief

    rows = [
        _row("top1", "今日头号大事", category="储能", importance="high"),
        _row("top2", "第二件大事", category="新能源", importance="high"),
        _row("other", "其他普通条目", category="产业链", importance="low"),
    ]
    plan = plan_entries(rows, ai=None)
    text = render_brief(plan, date_label="2026-08-26")

    assert text.count("http://www.nea.gov.cn/20260826/top1/c.html") == 1
    assert text.count("http://www.nea.gov.cn/20260826/top2/c.html") == 1
    assert "其他普通条目" in text
    assert "材料与产业链" in text


def test_brief_date_uses_shanghai_calendar_day() -> None:
    from intel_daily.digest import plan_entries, render_brief

    rows = [
        _row(
            "gx",
            "广期所测试条目",
            published_at="2026-08-25T16:00:00+00:00",
        ),
    ]
    plan = plan_entries(rows, ai=None)
    text = render_brief(plan, date_label="2026-08-27")

    assert "08-26" in text
    assert "08-25" not in text


def test_brief_carries_publish_date_per_item() -> None:
    from intel_daily.digest import plan_entries, render_brief

    rows = [
        _row("d1", "八月二十一日发布的新闻", published_at="2026-08-21T09:00:00+00:00"),
    ]
    plan = plan_entries(rows, ai=None)
    text = render_brief(plan, date_label="2026-08-26")

    assert "08-21" in text


def test_plan_entries_excludes_demo_preview_rows() -> None:
    from intel_daily.digest import plan_entries

    rows = [
        _row("r1", "真实新闻", importance="high"),
        _row(
            "r2",
            "国家能源局：上半年光伏新增装机7207万千瓦（样式预览）",
            importance="high",
        ),
        _row(
            "r3",
            "证监会发布私募基金监管新规",
            importance="high",
            summary="示例摘要：新规完善私募基金管理人登记。",
        ),
    ]
    plan = plan_entries(rows, ai=None)

    titles = [e.title for e in plan.total]
    assert "真实新闻" in titles
    assert all("样式预览" not in title for title in titles)
    assert all("示例摘要" not in e.summary for e in plan.total)
    assert len(plan.total) == 1


def test_plan_entries_filters_by_importance_levels() -> None:
    from intel_daily.digest import plan_entries

    rows = [
        _row("h", "重要政策", importance="high"),
        _row("m", "中等新闻", importance="medium"),
        _row("l", "一般消息", importance="low"),
    ]
    plan = plan_entries(rows, importance_levels=("high", "medium"))
    assert {e.importance for e in plan.total} == {"high", "medium"}
    assert len(plan.total) == 2

    all_levels = plan_entries(rows)
    assert len(all_levels.total) == 3


def test_render_brief_limited_drops_lowest_first() -> None:
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited

    highs = [
        _row(f"hi{i}", "重磅政策" * 8, importance="high") for i in range(2)
    ]
    lows = [
        _row(f"lo{i}", "一般动态" * 8, importance="low") for i in range(3)
    ]
    full = plan_entries(highs + lows)
    highs_only = plan_entries(highs)
    limit = len(render_brief(highs_only, date_label="2026-08-26")) + 20

    text, stats = render_brief_limited(full, date_label="2026-08-26", max_chars=limit)

    assert len(text) <= limit
    assert stats.trimmed >= 1
    assert stats.before_chars > stats.after_chars
    assert stats.within_limit is True
    for entry in highs_only.total:
        assert entry.title in text
    assert text.count("一般动态") < 3


def test_render_brief_limited_drops_oldest_within_same_level() -> None:
    # 同级别并列时应“更旧者先丢”，不能丢最新条目
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited

    old_low = _row(
        "old", "低优先旧闻内容", importance="low", id=101,
        published_at="2026-08-19T09:00:00+00:00",
    )
    new_low = _row(
        "new", "低优先新闻内容", importance="low", id=102,
        published_at="2026-08-25T09:00:00+00:00",
    )
    plan = plan_entries([old_low, new_low])
    limit = len(render_brief(plan_entries([new_low], ), date_label="2026-08-26")) + 20

    text, stats = render_brief_limited(plan, date_label="2026-08-26", max_chars=limit)

    assert "低优先新闻内容" in text
    assert "低优先旧闻内容" not in text
    assert stats.trimmed == 1


def test_render_brief_limited_trimmed_counts_entries_not_iterations() -> None:
    # 两条同级别同日期条目一轮同时被丢 → trimmed 应为 2（按条目数计）
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited

    rows = [
        _row(f"t{i}", "超长低优先条目内容", importance="low", id=200 + i,
             published_at="2026-08-21T09:00:00+00:00")
        for i in (1, 2)
    ]
    plan = plan_entries(rows)
    limit = len(render_brief(plan_entries([rows[0]]), date_label="2026-08-26"))

    text, stats = render_brief_limited(plan, date_label="2026-08-26", max_chars=limit)

    assert stats.trimmed == 2


def test_render_brief_limited_kept_ids_excludes_dropped_and_overflow() -> None:
    # 存活集：被 20K 裁剪掉的、以及“同类另有 N 条”溢出未渲染的都不算存活
    from intel_daily.digest import plan_entries, render_brief_limited

    highs = [
        _row(f"h{i}", "重磅消息" + str(i), importance="high", id=300 + i)
        for i in range(1)
    ]
    mediums = [
        _row(f"m{i}", "中等消息" + str(i), importance="medium", id=400 + i)
        for i in range(8)
    ]
    lows = [
        _row(f"l{i}", "低优先消息" + str(i), importance="low", id=500 + i)
        for i in range(2)
    ]
    plan = plan_entries(highs + mediums + lows)
    text, stats = render_brief_limited(plan, date_label="2026-08-26", max_chars=20000)

    # 存活 = 【今日重点】前 3 + 各块非重点前 per_block(3)
    top_entries = [e for e in plan.total if e.importance in ("high", "medium")][:3]
    top_urls = {e.url for e in top_entries}
    expected = {e.id for e in top_entries}
    for block_entries in plan.grouped.values():
        non_top = [e for e in block_entries if e.url not in top_urls]
        expected.update(e.id for e in non_top[:3])
    assert stats.kept_ids == frozenset(expected)
    assert len(stats.kept_ids) == 6
    overflow = {e.id for e in plan.total if e.id is not None} - stats.kept_ids
    # 低优先与块内溢出条目均不在存活集
    assert {500, 501} <= overflow


def test_render_brief_limited_kept_ids_excludes_char_trimmed() -> None:
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited

    rows = [
        _row(f"l{i}", "低优先长内容填充" * 4, importance="low", id=600 + i)
        for i in range(4)
    ] + [
        _row("h", "高优先重要政策", importance="high", id=610),
    ]
    plan = plan_entries(rows)
    limit = len(render_brief(plan_entries([rows[-1]]), date_label="2026-08-26")) + 40

    text, stats = render_brief_limited(plan, date_label="2026-08-26", max_chars=limit)

    assert 610 in stats.kept_ids
    assert stats.trimmed >= 1
    assert not {600, 601, 602, 603} & stats.kept_ids


def test_render_brief_limited_honests_overflow_when_header_only_exceeds() -> None:
    from intel_daily.digest import plan_entries, render_brief_limited

    plan = plan_entries([])
    text, stats = render_brief_limited(plan, date_label="2026-08-26", max_chars=50)

    assert stats.trimmed == 0
    assert stats.within_limit is False
    assert len(text) > 50


def test_render_brief_limited_no_trim_within_limit() -> None:
    from intel_daily.digest import plan_entries, render_brief, render_brief_limited

    rows = [_row("a", "普通条目", importance="medium") for _ in range(3)]
    plan = plan_entries(rows)
    text, stats = render_brief_limited(plan, date_label="2026-08-26")

    assert stats.trimmed == 0
    assert stats.within_limit is True
    assert text == render_brief(plan, date_label="2026-08-26")


def test_render_brief_limited_empty_plan_within_default() -> None:
    from intel_daily.digest import plan_entries, render_brief_limited

    plan = plan_entries([])
    text, stats = render_brief_limited(plan, date_label="2026-08-26")

    assert stats.trimmed == 0
    assert stats.within_limit is True
    assert "今日情报简报 2026-08-26" in text


def test_ai_rank_engine_uses_deepseek_contract(tmp_path) -> None:
    from intel_daily.digest import DigestEntry, DeepSeekRankEngine
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    captured = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured["body"] = json.loads(self.rfile.read(length))
            out = json.dumps({"choices": [{"message": {"content": json.dumps(
                {"ranking": [2, 0, 1], "notes": {"2": "价格波动显著", "0": "政策落地"}})} }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):  # noqa: D102
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        engine = DeepSeekRankEngine(api_key="test-key", base_url=f"http://127.0.0.1:{httpd.server_address[1]}")
        entries = [
            DigestEntry(title="A", summary="a", block="新能源与储能", importance="low", url="http://x/1", publisher="p", published_at="2026-08-25T00:00:00+00:00"),
            DigestEntry(title="B", summary="b", block="材料与产业链", importance="low", url="http://x/2", publisher="p", published_at="2026-08-25T00:00:00+00:00"),
            DigestEntry(title="C", summary="c", block="政策与监管", importance="low", url="http://x/3", publisher="p", published_at="2026-08-25T00:00:00+00:00"),
        ]
        ranked = engine.rank(entries)
    finally:
        httpd.shutdown()
    assert [e.title for e in ranked] == ["C", "A", "B"]
    assert ranked[0].note == "价格波动显著"
    assert "json" in captured["body"].get("response_format", {}).get("type", "")


def test_ai_rank_engine_failure_falls_back_to_none() -> None:
    from intel_daily.digest import DigestEntry, DeepSeekRankEngine

    entry = DigestEntry(title="A", summary="a", block="x", importance="low", url="http://x/1", publisher="p", published_at="2026-08-25T00:00:00+00:00")
    engine = DeepSeekRankEngine(api_key="test-key", base_url="http://127.0.0.1:1")
    assert engine.rank([entry]) is None


def test_fetch_items_honours_injected_now(tmp_path) -> None:
    """T7：窗口完全由注入的 `now` 决定，同一份数据在未来任意一天结果一致。"""
    import sqlite3

    from datetime import timedelta

    from intel_daily.db import init_database
    from intel_daily.notifier import count_window_items, fetch_items

    db = tmp_path / "intel.db"
    init_database(db)
    with sqlite3.connect(db) as con:
        con.execute(
            """INSERT INTO intelligence_items
            (source_id, external_id, title, original_url, canonical_url,
             publisher, published_at, fetched_at, source_type,
             verification_status, category, importance, summary,
             content_fingerprint, status)
            VALUES (3, 'n1', '注入口期条目', 'http://x/n1', 'http://x/n1',
                    '国家能源局', '2026-08-25T09:00:00+00:00',
                    '2026-08-25T09:30:00+00:00', 'primary', 'primary', '储能',
                    'high', 's', 'sha256:n1', 'collected')"""
        )

    def ids_on(day: datetime) -> set[str]:
        return {row["external_id"] for row in fetch_items(db, days=3, now=day)}

    assert ids_on(datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)) == {"n1"}
    assert ids_on(datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)) == {"n1"}
    # 窗口之外（含“未来某一天”）：不再出现，且计数同样随注入时钟变化
    assert ids_on(datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)) == set()
    assert count_window_items(db, days=3, now=datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)) == 1
    assert count_window_items(db, days=3, now=datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)) == 0
    # 未注入时用真实时钟（不得再钉死 2026-08-26）
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    with sqlite3.connect(db) as con:
        con.execute(
            """INSERT INTO intelligence_items
            (source_id, external_id, title, original_url, canonical_url,
             publisher, published_at, fetched_at, source_type,
             verification_status, category, importance, summary,
             content_fingerprint, status)
            VALUES (3, 'n2', '真实时钟条目', 'http://x/n2', 'http://x/n2',
                    '国家能源局', ?, ?, 'primary', 'primary', '储能',
                    'high', 's', 'sha256:n2', 'collected')""",
            (fresh.isoformat(), fresh.isoformat()),
        )
    assert "n2" in {row["external_id"] for row in fetch_items(db, days=3)}


def test_fetch_items_excludes_sent_and_respects_window(tmp_path) -> None:
    import sqlite3

    from intel_daily.db import init_database
    from intel_daily.notifier import fetch_items

    db = tmp_path / "intel.db"
    init_database(db)
    with sqlite3.connect(db) as con:
        for ext, pub, status in (
            ("w1", "2026-08-25T09:00:00+00:00", "collected"),
            ("w2", "2026-08-20T09:00:00+00:00", "collected"),
            ("w3", "2026-08-25T09:00:00+00:00", "sent"),
        ):
            con.execute(
                """INSERT INTO intelligence_items
                (source_id, external_id, title, original_url, canonical_url,
                 publisher, published_at, fetched_at, source_type,
                 verification_status, category, importance, summary,
                 content_fingerprint, status)
                VALUES (3, ?, ?, ?, ?, '国家能源局', ?, '2026-08-26T00:00:00+00:00',
                        'primary', 'primary', '储能', 'high', 's', ?, ?)""",
                (ext, f"标题{ext}", f"http://x/{ext}", f"http://x/{ext}", pub, f"sha256:{ext}", status),
            )

    items = fetch_items(db, days=3, now=NOW)
    assert {i["external_id"] for i in items} == {"w1"}
    # 默认真实时钟 + 相对日期：未来任意一天运行同样全绿
    from datetime import timedelta

    today = datetime.now(timezone.utc)
    recent = "2026-08-25T09:00:00+00:00"
    with sqlite3.connect(db) as con:
        con.execute(
            """INSERT INTO intelligence_items
            (source_id, external_id, title, original_url, canonical_url,
             publisher, published_at, fetched_at, source_type,
             verification_status, category, importance, summary,
             content_fingerprint, status)
            VALUES (3, 'rel1', '相对日期条目', 'http://x/rel1', 'http://x/rel1',
                    '国家能源局', ?, ?, 'primary', 'primary', '储能', 'high',
                    's', 'sha256:rel1', 'collected')""",
            (
                (today - timedelta(days=1)).isoformat(),
                today.isoformat(),
            ),
        )
    relative = fetch_items(db, days=3)
    assert "rel1" in {i["external_id"] for i in relative}
    assert recent not in {i["published_at"] for i in relative}
