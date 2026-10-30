import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    received: dict = {}
    response_payload: dict = {}
    status: int = 200

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "model": body.get("model"),
            "user_text": self._user_text(body),
        }
        if self.status != 200:
            body_bytes = b'{"error": "boom"}'
            self.send_response(self.status)
        else:
            body_bytes = json.dumps(self.response_payload).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    @staticmethod
    def _user_text(body: dict) -> str:
        for message in body.get("messages", []):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    def log_message(self, *args) -> None:  # noqa: D102
        pass


class _Server:
    def __init__(self, payload: dict, status: int = 200) -> None:
        handler = type(
            "BoundHandler",
            (_Handler,),
            {"response_payload": payload, "status": status},
        )
        self.handler_class = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def received(self) -> dict:
        return self.handler_class.received.copy()

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _payload(summary: str) -> dict:
    return {"choices": [{"message": {"content": summary}}]}


def test_stub_summarizer_never_calls_model() -> None:
    from intel_daily.summarizers import StubSummarizer

    result = StubSummarizer().generate("新型储能装机1.53亿千瓦")
    assert result.summary is None
    assert result.provider == "stub"
    assert result.error is None


def test_deepseek_summarizer_chat_completions_contract() -> None:
    from intel_daily.summarizers import DeepSeekSummarizer

    text = "新型储能成为第一大调节性资源 截至6月底全国新型储能装机达1.53亿千瓦"
    with _Server(_payload("上半年我国新型储能装机1.53亿千瓦，同比大幅增长。")) as server:
        result = DeepSeekSummarizer(
            api_key="test-deepseek-key", base_url=server.base_url
        ).generate(text)

    assert result.summary == "上半年我国新型储能装机1.53亿千瓦，同比大幅增长。"
    assert result.provider == "deepseek"
    assert result.error is None
    received = server.received
    assert received["path"] == "/chat/completions"
    assert received["authorization"] == "Bearer test-deepseek-key"
    assert received["model"] == "deepseek-chat"
    assert text in received["user_text"]


def test_deepseek_summarizer_error_paths() -> None:
    from intel_daily.summarizers import DeepSeekSummarizer

    with _Server(_payload("占位"), status=500) as server:
        result = DeepSeekSummarizer(
            api_key="test-deepseek-key", base_url=server.base_url
        ).generate("正文")
    assert result.summary is None
    assert result.error is not None
    assert "test-deepseek-key" not in result.error

    with _Server({"choices": [{"message": {"content": ""}}]}) as server:
        result = DeepSeekSummarizer(
            api_key="test-deepseek-key", base_url=server.base_url
        ).generate("正文")
    assert result.summary is None
    assert result.error is not None
    with _Server({"choices": [{"message": {}}]}) as server:
        result = DeepSeekSummarizer(
            api_key="test-deepseek-key", base_url=server.base_url
        ).generate("正文")
    assert result.summary is None
    assert result.error is not None
