import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

DEFAULT_SYSTEM_PROMPT = (
    "你是公司内部新能源情报助手。根据提供的标题与来源摘要，输出 2 至 4 句"
    "事实性中文短摘要。不得添加原文不存在的事实、数字或结论，不得推测，"
    "不得给出评级或投资建议。只输出摘要本身。"
)
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_BASE_URL = "https://api.deepseek.com"
_REQUEST_TIMEOUT = 30
_MAX_INPUT_CHARS = 8000


@dataclass(frozen=True)
class SummaryResult:
    summary: str | None
    provider: str
    error: str | None = None


class Summarizer(Protocol):
    def generate(self, text: str) -> SummaryResult: ...


class StubSummarizer:
    """默认摘要器：不调用任何模型；摘要由官方 summary 字段承担。"""

    def generate(self, text: str) -> SummaryResult:
        return SummaryResult(summary=None, provider="stub")


class DeepSeekSummarizer:
    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def generate(self, text: str) -> SummaryResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": text[:_MAX_INPUT_CHARS]},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "intel-daily/0.1 (+internal daily intelligence)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            return SummaryResult(
                summary=None,
                provider="deepseek",
                error=f"DeepSeek 接口返回 {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            return SummaryResult(
                summary=None,
                provider="deepseek",
                error=f"DeepSeek 接口不可达: {exc}",
            )
        try:
            data = json.loads(body.decode("utf-8"))
            content = data["choices"][0]["message"].get("content", "")
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, IndexError, TypeError) as exc:
            return SummaryResult(
                summary=None,
                provider="deepseek",
                error=f"DeepSeek 响应解析失败: {exc}",
            )
        summary = content.strip()
        if not summary:
            return SummaryResult(
                summary=None,
                provider="deepseek",
                error="DeepSeek 返回空摘要",
            )
        return SummaryResult(summary=summary, provider="deepseek")
