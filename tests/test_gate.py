import pytest

from intel_daily.gate import evidence_passes


def test_evidence_passes_complete_item() -> None:
    result = evidence_passes(
        title="新型储能成为第一大调节性资源",
        original_url="http://www.nea.gov.cn/20260821/f3fa895e/c.html",
        published_at="2026-08-21T17:37:04+08:00",
        publisher="国家能源局",
    )
    assert result.passed is True
    assert result.missing == ()


@pytest.mark.parametrize(
    ("kwargs", "expected_missing"),
    [
        ({"title": "   ", "original_url": "http://x/y", "published_at": "2026-08-21T00:00:00Z", "publisher": "X"}, ("title",)),
        ({"title": "T", "original_url": "", "published_at": "2026-08-21T00:00:00Z", "publisher": "X"}, ("original_url",)),
        ({"title": "T", "original_url": "http://x/y", "published_at": "  ", "publisher": "X"}, ("published_at",)),
        ({"title": "T", "original_url": "http://x/y", "published_at": "2026-08-21T00:00:00Z", "publisher": None}, ("publisher",)),
    ],
)
def test_evidence_gate_rejects_missing_fields(kwargs, expected_missing) -> None:
    result = evidence_passes(**kwargs)
    assert result.passed is False
    assert result.missing == expected_missing


@pytest.mark.parametrize(
    "original_url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html;base64,PHNjcmlwdD4=",
        "ftp://files.example.gov.cn/a.pdf",
        "mailto:someone@example.com",
        "//www.nea.gov.cn/x/c.html",
        "_NEA_://x",
    ],
)
def test_evidence_gate_rejects_non_http_scheme(original_url) -> None:
    """T11：只有 http/https 链接可追溯，其他协议视为证据不足（missing 含 original_url）。"""
    result = evidence_passes(
        title="储能试点方案",
        original_url=original_url,
        published_at="2026-08-21T17:37:04+08:00",
        publisher="国家能源局",
    )
    assert result.passed is False
    assert result.missing == ("original_url",)


@pytest.mark.parametrize(
    "original_url",
    [
        "http://www.nea.gov.cn/x/c.html",
        "https://www.nea.gov.cn/x/c.html",
        "HTTPS://WWW.NEA.GOV.CN/x/c.html",
    ],
)
def test_evidence_gate_accepts_http_and_https(original_url) -> None:
    result = evidence_passes(
        title="储能试点方案",
        original_url=original_url,
        published_at="2026-08-21T17:37:04+08:00",
        publisher="国家能源局",
    )
    assert result.passed is True
