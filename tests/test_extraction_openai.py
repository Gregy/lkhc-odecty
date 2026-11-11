"""openai_compat extractor against a local stub HTTP server (no real model involved)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app.extraction.extractors import OpenAICompatExtractor
from app.extraction.types import ExtractionHints, MeterHintIn
from tests.capture_helpers import make_jpeg


class Stub:
    """Scripted responses: each item is (status, body, headers, delay_s)."""

    def __init__(self) -> None:
        self.script: list[tuple[int, Any, dict[str, str], float]] = []
        self.requests: list[dict[str, Any]] = []


def chat(content: str, model: str = "stub-vision") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 1},
    }


GOOD = json.dumps(
    {
        "readable": True,
        "problems": [],
        "meters": [
            {
                "serial": "69253071",
                "serial_confidence": 0.9,
                "kind": "lcd",
                "registers": [
                    {"label_seen": "", "register": "T1", "digits_seen": "0051237", "value": 512.37, "confidence": 0.92}
                ],
            }
        ],
    }
)


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length))
            state.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, payload, headers, delay = state.script.pop(0) if state.script else (500, "no script", {}, 0)
            if delay:
                time.sleep(delay)
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            try:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError, ConnectionResetError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


HINTS = ExtractionHints(
    meters=(MeterHintIn(meter_id=10, serial="69253071", point_name="Nájemce A z chodby", decimals_shown=2),)
)
IMG = make_jpeg()


def ex(url: str, **kw: Any) -> OpenAICompatExtractor:
    sleeps: list[float] = []
    e = OpenAICompatExtractor(url, "stub-vision", "secret-key", sleep=sleeps.append, **kw)
    e.sleeps = sleeps  # type: ignore[attr-defined]
    return e


def test_happy_path_and_request_shape(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script.append((200, chat(f"```json\n{GOOD}\n```"), {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert res.ok and res.readable and res.model == "stub-vision" and res.latency_ms is not None
    r = res.meters[0].registers[0]
    assert res.meters[0].serial == "69253071" and r.digits_seen == "0051237" and r.value == D("512.37")
    req = state.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer secret-key"
    body = req["body"]
    assert (
        body["model"] == "stub-vision"
        and body["temperature"] == 0
        and body["response_format"] == {"type": "json_object"}
    )
    parts = body["messages"][1]["content"]
    assert "69253071" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_malformed_json_is_an_error_not_a_crash(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script.append((200, chat('{"readable": true, "meters": [ {"serial": '), {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and res.problems == ["model_no_json"] and not res.meters
    state.script.append((200, "<html>proxy error</html>", {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and "model_no_json" in res.problems


def test_answer_cut_off_by_max_tokens_keeps_usage(stub: tuple[Stub, str]) -> None:
    # reasoning models spend tokens before the JSON; a cut-off answer must be reported as such, with its cost
    state, url = stub
    # cut inside the meters list: the first *complete* object is one meter - not the answer
    cut = '{"readable": true, "meters": [ {"serial": null, "registers": [{"digits_seen": "1"}]}, {"ser'
    body = chat(cut)
    body["choices"][0]["finish_reason"] = "length"
    body["usage"] = {"prompt_tokens": 3000, "completion_tokens": 4000, "cost": 0.01}
    state.script.append((200, body, {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and res.problems == ["model_no_json"] and "max_tokens" in (res.error or "")
    assert res.raw["usage"]["cost"] == 0.01
    assert state.requests[0]["body"]["max_tokens"] >= 8000
    # the same text without finish_reason is still not mistaken for an (empty) answer
    state.script.append((200, chat(cut), {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and res.problems == ["model_no_json"]


def test_refusal_text(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script.append((200, chat("I'm sorry, but I can't help with identifying this device."), {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and res.problems == ["model_refused"]
    state.script.append((200, {"choices": [{"message": {"content": None, "refusal": "nope"}}]}, {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert res.problems == ["model_refused"]


def test_429_then_success_honours_retry_after(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script += [(429, {"error": "rate"}, {"Retry-After": "7"}, 0), (200, chat(GOOD), {}, 0)]
    e = ex(url)
    res = e.extract(IMG, HINTS)
    assert res.ok and len(state.requests) == 2
    assert e.sleeps == [7.0]  # type: ignore[attr-defined]


def test_429_without_retry_after_waits_for_a_rate_window(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script += [(429, {"error": "rate"}, {}, 0)] * 2 + [(200, chat(GOOD), {}, 0)]
    e = ex(url)
    res = e.extract(IMG, HINTS)
    assert res.ok and len(state.requests) == 3
    assert [round(x) for x in e.sleeps] in ([10, 20], [11, 20], [10, 21], [11, 21])  # type: ignore[attr-defined]


def test_persistent_500_gives_up_after_bounded_retries(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script += [(500, "boom", {}, 0)] * 5
    e = ex(url, max_retries=2)
    res = e.extract(IMG, HINTS)
    assert not res.ok and "HTTP 500" in (res.error or "")
    assert len(state.requests) == 3 and len(e.sleeps) == 2  # type: ignore[attr-defined]


def test_400_is_not_retried(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script.append((401, {"error": "bad key"}, {}, 0))
    res = ex(url).extract(IMG, HINTS)
    assert not res.ok and "HTTP 401" in (res.error or "") and len(state.requests) == 1


def test_server_without_response_format_support(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script += [(400, {"error": "unknown field response_format"}, {}, 0), (200, chat(GOOD), {}, 0)]
    res = ex(url).extract(IMG, HINTS)
    assert res.ok and "response_format" not in state.requests[1]["body"]


def test_timeout(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.script += [(200, chat(GOOD), {}, 1.5)] * 2
    e = ex(url, timeout=0.3, max_retries=1)
    t0 = time.monotonic()
    res = e.extract(IMG, HINTS)
    assert not res.ok and res.problems == ["timeout"]
    assert time.monotonic() - t0 < 5


def test_connection_refused_never_raises() -> None:
    res = ex("http://127.0.0.1:9/v1", max_retries=1).extract(IMG, HINTS)
    assert not res.ok and res.error and "connection" in res.error


def test_missing_config() -> None:
    res = OpenAICompatExtractor("", "").extract(IMG, HINTS)
    assert not res.ok and "EXTRACTOR_BASE_URL" in (res.error or "")
