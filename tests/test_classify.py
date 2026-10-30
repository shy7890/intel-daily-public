import pytest

from intel_daily.classify import classify_text


@pytest.mark.parametrize(
    ("title", "summary", "keywords", "expected_category"),
    [
        ("新型储能成为第一大调节性资源", "", ("储能",), "储能"),
        ("国家能源局关于印发新型储能试点工作方案的通知", "", (), "储能"),
        ("上半年全国光伏新增装机7207万千瓦", "增长约XX%", ("光伏",), "新能源"),
        ("风电利用率达到90.9%", "", ("风电",), "新能源"),
        ("私募基金管理人登记备案办法", "", (), "私募监管"),
        ("碳酸锂价格周报", "价格下跌", (), "产业链"),
        ("电力现货市场运行规则", "", (), "电力市场"),
        ("某公益慈善会议召开", "", (), "行业动态"),
    ],
)
def test_category_rules(title, summary, keywords, expected_category) -> None:
    result = classify_text(title, summary, keywords)
    assert result.category == expected_category


def test_policy_markers_raise_importance_to_high() -> None:
    result = classify_text(
        "国家能源局关于组织开展新型电力系统建设试点的通知",
        "",
        ("电力系统",),
    )
    assert result.category == "电力市场"
    assert result.importance == "high"


def test_pure_data_posts_are_medium() -> None:
    result = classify_text(
        "2026年上半年全国可再生能源新增装机1.17亿千瓦",
        "占全部新增装机的73.9%",
        (),
    )
    assert result.category == "新能源"
    assert result.importance == "medium"


def test_unrelated_items_are_low() -> None:
    result = classify_text("一些无关的会议通知", "", ())
    assert result.category == "行业动态"
    assert result.importance == "low"


def test_classification_is_stable_across_ws_and_punct() -> None:
    first = classify_text("储能 政策", "", ())
    second = classify_text("储能政策", "", ())
    assert first == second


def test_shfe_contract_title_maps_to_supply_chain() -> None:
    result = classify_text(
        "沪期货 镍：主力合约结算价 129520（2026-08-26）",
        "",
        ("镍",),
    )
    assert result.category == "产业链"
