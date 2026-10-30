import re
from dataclasses import dataclass

_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("储能", ("储能", "电化学储能", "新型储能占比", "长时储能")),
    ("产业链", ("碳酸锂", "锂电", "锂电池", "正极", "负极", "隔膜", "电解液", "镍钴", "锂矿", "沪镍", "期镍", "沪锡", "期锡", "沪期货")),
    ("电力市场", ("电力市场", "现货", "电价", "峰谷", "电力系统")),
    ("新能源", ("光伏", "风电", "新能源装机", "可再生能源", "风光", "新能源发电")),
    ("私募监管", ("私募", "基金备案", "基金业协会", "资产管理", "管理人")),
)

POLICY_MARKERS = (
    "国务院",
    "国家发展改革委",
    "国家能源局",
    "证监会",
    "通知",
    "办法",
    "方案",
    "规划",
    "试点",
    "意见",
    "规定",
    "印发",
    "实施细则",
    "公告",
)


@dataclass(frozen=True)
class Classification:
    category: str
    importance: str


def classify_text(
    title: str, summary: str, keywords: tuple[str, ...]
) -> Classification:
    text = _WS_RE.sub(
        " ", f"{title} {summary} {' '.join(keywords)}".strip()
    )
    category = "行业动态"
    for candidate, markers in CATEGORY_RULES:
        if any(marker in text for marker in markers):
            category = candidate
            break
    importance = _importance(text, category)
    return Classification(category=category, importance=importance)


def _importance(text: str, category: str) -> str:
    if any(marker in text for marker in POLICY_MARKERS) and category != "行业动态":
        return "high"
    if _DIGIT_RE.search(text):
        return "medium"
    return "low"
