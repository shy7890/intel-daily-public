from dataclasses import replace

from intel_daily.digest import DigestEntry, plan_entries, render_brief_limited
from intel_daily.similarity import DeepSeekSimilarityMerger, merge_similar


def _entry(idx, title, publisher="来源A", published="2026-08-27T02:00:00+00:00", summary="内容"):  # noqa: ANN001
    return DigestEntry(
        title=title,
        summary=f"摘要{idx} {summary}",
        block="公司动态",
        importance="high",
        url=f"https://example.com/{idx}",
        publisher=publisher,
        published_at=published,
        id=idx,
    )


def test_merge_merges_same_title_across_sources_by_normalized_equals():
    a = _entry(1, "新能源企业发布2026年度中报")
    b = _entry(2, "新能源企业发布 2026 年度中报！", publisher="媒体X")
    result = merge_similar([a, b])
    assert len(result.entries) == 1
    assert result.removed == 1
    merged = result.entries[0]
    assert merged.id == 1
    assert merged.publisher == "来源A"
    assert ("媒体X", "https://example.com/2", b.published_at) in merged.related_links


def test_merge_keeps_distinct_titles():
    a = _entry(1, "宁德时代发布新电池公告")
    b = _entry(2, "贵州省发布储能政策")
    result = merge_similar([a, b])
    assert len(result.entries) == 2
    assert result.removed == 0


def test_merge_ai_groups_similar_but_not_identical_titles():
    class FakeAi:
        def find_groups(self, entries):
            return [[0, 2]]

    a = _entry(1, "国家能源局公布1-7月用电量数据", published="2026-08-26T02:00:00+00:00")
    b = _entry(2, "贵州新能源电池项目开工")
    c = _entry(3, "能源局发布1-7月份全社会用电量", publisher="国家能源局", published="2026-08-27T02:00:00+00:00")
    result = merge_similar([a, b, c], ai=FakeAi())
    assert len(result.entries) == 2
    assert result.removed == 1
    canonical = [e for e in result.entries if e.id == 1][0]
    assert ("国家能源局", "https://example.com/3", c.published_at) in canonical.related_links


def test_merge_ai_none_falls_back_to_deterministic_only():
    ai = type("FailingAi", (), {"find_groups": lambda self, entries: None})()
    a = _entry(1, "某公司公告")
    b = _entry(2, "完全不同的事件")
    result = merge_similar([a, b], ai=ai)
    assert len(result.entries) == 2
    assert result.removed == 0


def test_merge_canonical_is_earliest_published_then_first_seen():
    early = _entry(1, "同一标题", published="2026-08-26T02:00:00+00:00")
    late = _entry(2, "同一标题", published="2026-08-27T02:00:00+00:00")
    result = merge_similar([late, early])
    assert result.entries[0].id == 1
    assert result.entries[0].published_at == "2026-08-26T02:00:00+00:00"


def test_render_shows_related_source_and_link():
    a = _entry(1, "重磅政策发布")
    b = _entry(2, "重磅政策发布", publisher="媒体X")
    plan = plan_entries(
        [
            {"title": e.title, "summary": e.summary, "block": "宏观政策", "importance": "high",
             "original_url": e.url, "publisher": e.publisher, "published_at": e.published_at,
             "note": "", "id": e.id, "related_links": e.related_links}
            for e in merge_similar([a, b]).entries
        ]
    )
    text, _ = render_brief_limited(plan, date_label="2026-08-27")
    assert "另见" in text
    assert "媒体X" in text
    assert "https://example.com/2" in text


def test_plan_entries_applies_merger_when_provided():
    class FakeAi:
        def find_groups(self, entries):
            return [[0, 1]]

    rows = [
        {"title": t, "summary": "s", "block": "宏观政策", "importance": "high",
         "original_url": f"https://x.com/{i}", "publisher": p, "published_at": d,
         "note": "", "id": i}
        for i, (t, p, d) in enumerate([
            ("能源局发布用电量统计", "国家能源局", "2026-08-27T02:00:00+00:00"),
            ("能源局发布用电量统计", "媒体转载", "2026-08-27T03:00:00+00:00"),
        ])
    ]
    plan = plan_entries(rows, ai=None, merger=FakeAi())
    assert len(plan.total) == 1
    assert plan.total[0].id == 1 or plan.total[0].id == 0


def test_merge_records_absorbed_ids_of_dropped_duplicates():
    # 被合并（替身）条目 id 必须随结果返回，供发送后与 canonical 一起 mark_sent
    a = _entry(1, "新能源企业发布2026年度中报")
    b = _entry(2, "新能源企业发布 2026 年度中报！", publisher="媒体X")
    result = merge_similar([a, b])
    assert result.absorbed_ids == (2,)
    assert result.absorbed_of == ((1, 2),)


def test_merge_absorbed_ids_follow_ai_groups_and_skip_none_ids():
    class FakeAi:
        def find_groups(self, entries):
            return [[0, 2]]

    a = _entry(1, "国家能源局公布1-7月用电量数据", published="2026-08-26T02:00:00+00:00")
    b = _entry(2, "贵州新能源电池项目开工")
    c = _entry(None, "能源局发布1-7月份全社会用电量", publisher="国家能源局",
               published="2026-08-27T02:00:00+00:00")
    result = merge_similar([a, b, c], ai=FakeAi())
    assert result.absorbed_ids == ()  # id=None 的替身不参与标记
    assert result.absorbed_of == ()


def test_plan_entries_propagates_absorbed_ids():
    from intel_daily.digest import plan_entries

    rows = [
        {"title": "同一公告", "summary": "s", "category": "公司动态", "importance": "high",
         "original_url": "https://x.com/1", "publisher": "来源A",
         "published_at": "2026-08-27T02:00:00+00:00", "id": 11},
        {"title": "同一公告", "summary": "s", "category": "公司动态", "importance": "high",
         "original_url": "https://x.com/2", "publisher": "来源B",
         "published_at": "2026-08-27T03:00:00+00:00", "id": 22},
    ]
    # AI 分组失败（None）时仍有确定性合并，absorbed 信息必须随之传递
    merger = type("DeterministicOnly", (), {"find_groups": lambda self, e: None})()
    plan = plan_entries(rows, merger=merger)
    assert [e.id for e in plan.total] == [11]
    assert plan.absorbed_ids == (22,)
    assert plan.absorbed_of == ((11, 22),)


def test_deepseek_merger_group_indices_validated():
    class FakeSocket:
        pass

    merger = DeepSeekSimilarityMerger(api_key="k")
    entries = [_entry(i, f"标题{i}") for i in range(3)]
    assert merger.parse_groups({"groups": [[0, 2], [1]], "extra": "x"}, entries) == [[0, 2]]
    assert merger.parse_groups({"groups": [[0, 5], [1]], "extra": "x"}, entries) == []
    assert merger.parse_groups({"groups": ["a", [0, 1]], "extra": "x"}, entries) == [[0, 1]]
