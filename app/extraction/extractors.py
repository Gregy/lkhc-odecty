"""Extractor implementations: ``none``, ``fake`` and ``openai_compat``.

``openai_compat`` talks to any OpenAI-compatible ``/chat/completions`` endpoint that accepts
images (OpenRouter, Ollama ``/v1``, llama.cpp server, vLLM, ...). Every implementation
returns an ``ExtractionResult`` and never raises.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from app.extraction.matching import normalize_serial
from app.extraction.prompt import SYSTEM_PROMPT, ParseError, build_prompt, extract_json_object, parse_answer
from app.extraction.types import (
    ExtractionHints,
    ExtractionResult,
    MeterReadout,
    ReadingExtractor,
    RegisterValue,
)

log = logging.getLogger(__name__)


class NoneExtractor:
    """No automatic extraction: photos are read by a human."""

    name = "none"

    def extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult:
        return ExtractionResult(readable=False, problems=[], model="none", error="extraction disabled")


class FakeExtractor:
    """Deterministic extractor for tests and demos.

    Lookup order for an image (by ``hints.image_sha256`` = sha256 of the stored original, else of the bytes):
    1. ``responses[sha]`` (dict in ``ExtractionResult.to_dict`` / model-JSON shape, or ExtractionResult),
    2. JSON file named by env ``EXTRACTOR_FAKE_RESPONSES`` ({sha256: answer}),
    3. ``hook(image_bytes, hints)`` if set,
    4. with a focus meter (walk photo): that meter's expected value + 1 kWh per register,
    5. otherwise "unreadable".
    Model-JSON shaped answers go through the same parser as real model output.
    """

    name = "fake"

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.hook: Callable[[bytes, ExtractionHints], ExtractionResult | dict[str, Any] | None] | None = None
        self.calls: list[str] = []

    def _file_responses(self) -> dict[str, Any]:
        path = os.environ.get("EXTRACTOR_FAKE_RESPONSES")
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except OSError, ValueError:
            log.warning("cannot read EXTRACTOR_FAKE_RESPONSES=%s", path)
            return {}

    def extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult:
        sha = hints.image_sha256 or hashlib.sha256(image_bytes).hexdigest()
        self.calls.append(sha)
        ans: Any = self.responses.get(sha)
        if ans is None:
            ans = self._file_responses().get(sha)
        if ans is None and self.hook is not None:
            ans = self.hook(image_bytes, hints)
        if isinstance(ans, ExtractionResult):
            return ans
        if isinstance(ans, dict):
            if "version" in ans:
                res = ExtractionResult.from_dict(ans) or ExtractionResult()
            else:
                res = parse_answer(ans)
            res.model = res.model or "fake"
            return res
        if hints.focus_meter_ids:
            by_id = hints.by_id()
            meters = []
            for mid in hints.focus_meter_ids:
                h = by_id.get(mid)
                if h is None:
                    continue
                regs = []
                for reg in h.registers:
                    rh = h.reg(reg)
                    base = (
                        rh.expected if rh and rh.expected is not None else (rh.last_value if rh else None)
                    ) or Decimal(0)
                    v = (base + 1).quantize(Decimal(1).scaleb(-h.decimals_shown))
                    regs.append(RegisterValue(reg, v, 0.9, reg, str(v), []))
                meters.append(MeterReadout(serial=h.serial, serial_confidence=0.9, registers=regs, kind="lcd"))
            if meters:
                return ExtractionResult(meters=meters, readable=True, model="fake", latency_ms=1)
        return ExtractionResult(readable=False, problems=["unreadable"], model="fake", latency_ms=1)


class OpenAICompatExtractor:
    """Vision model behind an OpenAI-compatible chat/completions API.

    ``verify`` (EXTRACTOR_VERIFY): every read that yielded digits is followed by a second, independent read of
    the same image (see ``compare_reads``); usage and latency then cover both calls.
    """

    name = "openai_compat"
    RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
        max_tokens: int = 8000,  # reasoning models think first (measured: up to ~3900 tokens before the JSON)
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        verify: bool = False,
    ) -> None:
        self.verify = verify
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.transport = transport
        self.sleep = sleep
        self.use_response_format = True

    @property
    def url(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _payload(self, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                },
            ],
        }
        if self.use_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "X-Title": "LKHC Odecty"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _backoff(self, attempt: int, retry_after: str | None, status: int | None = None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 60.0))
            except ValueError:
                pass
        if status == 429:  # per-minute limits (OpenRouter: 20 requests/min on new accounts) need longer waits
            return min(10.0 * 2.0 ** (attempt - 1) + random.uniform(0, 1), 60.0)
        return min(2.0**attempt + random.uniform(0, 0.5), 30.0)

    def extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult:
        started = time.monotonic()
        try:
            res = self._extract(image_bytes, hints)
        except Exception as exc:  # never raise into callers
            log.exception("extractor crashed")
            res = ExtractionResult(problems=["model_error"], error=f"{type(exc).__name__}: {exc}")
        if self.verify and res.ok and has_reading(res):
            self._verify(res, image_bytes, hints)
        res.model = res.model or self.model
        res.latency_ms = int((time.monotonic() - started) * 1000)  # both calls
        return res

    def _verify(self, res: ExtractionResult, image_bytes: bytes, hints: ExtractionHints) -> None:
        """Second, independent read of the same image with the same prompt (the model never sees its first
        answer); values it does not reproduce become doubtful. A failed second request leaves every value
        unverified = doubtful - the photo itself stays a successful extraction."""
        try:
            second = self._extract(image_bytes, hints)
        except Exception as exc:
            log.exception("verification read crashed")
            second = ExtractionResult(problems=["model_error"], error=f"{type(exc).__name__}: {exc}")
        try:
            comparisons = compare_reads(res, second)
        except Exception as exc:  # a bug in the comparison must not lose the first read
            log.exception("verification comparison crashed")
            second = ExtractionResult(problems=["model_error"], error=f"comparison failed: {type(exc).__name__}")
            comparisons = compare_reads(res, second)
        raw = res.raw if isinstance(res.raw, dict) else {"content": res.raw}
        second_raw = second.raw if isinstance(second.raw, dict) else {"content": second.raw}
        second_usage = second_raw.get("usage")
        raw["usage"] = sum_usage(raw.get("usage"), second_usage)  # both calls: what the photo cost
        raw["calls"] = 2
        raw["verify"] = {
            "content": second_raw.get("content"),
            "usage": second_usage,  # the second call alone (already included in raw["usage"])
            "error": second.error,
            "agreed": sum(1 for c in comparisons if c["agreed"]),
            "doubtful": sum(1 for c in comparisons if not c["agreed"]),
            "registers": comparisons,
        }
        res.raw = raw

    def _extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult:
        if not self.base_url or not self.model:
            return ExtractionResult(problems=["model_error"], error="EXTRACTOR_BASE_URL / EXTRACTOR_MODEL not set")
        prompt = build_prompt(hints)
        last_error = "unknown error"
        last_code = "model_error"
        attempt = 0
        with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0), transport=self.transport) as client:
            while attempt <= self.max_retries:
                try:
                    resp = client.post(self.url, headers=self._headers(), json=self._payload(image_bytes, prompt))
                except httpx.TimeoutException:
                    last_error, last_code = f"timeout after {self.timeout:.0f} s", "timeout"
                    attempt += 1
                    if attempt <= self.max_retries:
                        self.sleep(self._backoff(attempt, None))
                    continue
                except httpx.HTTPError as exc:
                    last_error, last_code = f"connection error: {exc}", "model_error"
                    attempt += 1
                    if attempt <= self.max_retries:
                        self.sleep(self._backoff(attempt, None))
                    continue
                if resp.status_code == 400 and self.use_response_format and "response_format" in resp.text:
                    # some servers do not support response_format: retry once without it (not counted)
                    self.use_response_format = False
                    continue
                if resp.status_code in self.RETRY_STATUS:
                    last_error, last_code = f"HTTP {resp.status_code}: {resp.text[:200]}", "model_error"
                    attempt += 1
                    if attempt <= self.max_retries:
                        self.sleep(self._backoff(attempt, resp.headers.get("retry-after"), resp.status_code))
                    continue
                if resp.status_code >= 400:
                    return ExtractionResult(
                        problems=["model_error"], error=f"HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                return self._parse_response(resp)
        return ExtractionResult(problems=[last_code], error=last_error)

    def _parse_response(self, resp: httpx.Response) -> ExtractionResult:
        try:
            body = resp.json()
        except ValueError:
            return ExtractionResult(problems=["model_no_json"], error="response is not JSON", raw=resp.text[:2000])
        try:
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # some servers return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            refusal = message.get("refusal")
        except AttributeError, IndexError, TypeError:
            return ExtractionResult(problems=["model_no_json"], error="unexpected response shape", raw=body)
        model = str(body.get("model") or self.model)
        usage = body.get("usage") if isinstance(body, dict) else None
        if refusal and not content:
            return ExtractionResult(
                problems=["model_refused"], error=f"refused: {str(refusal)[:200]}", raw=body, model=model
            )
        raw = {"content": content, "usage": usage}
        if choice.get("finish_reason") == "length":  # e.g. reasoning tokens used up max_tokens
            msg = f"answer cut off at max_tokens={self.max_tokens}"
            return ExtractionResult(problems=["model_no_json"], error=msg, raw=raw, model=model)
        try:
            obj = extract_json_object(content or "")
        except ParseError as exc:
            return ExtractionResult(problems=[exc.code], error=f"{exc.code}: {exc}", raw=raw, model=model)
        res = parse_answer(obj)
        res.raw = {"content": content, "usage": usage}
        res.model = model
        return res


# --- second read (EXTRACTOR_VERIFY) ------------------------------------------------------------

DOUBT_CONFIDENCE = 0.5  # cap for a value the second read did not reproduce (below matching.LOW_CONFIDENCE)


def _usable_digits(rv: RegisterValue) -> str:
    d = rv.digits_seen
    return d if d and "?" not in d and any(ch.isdigit() for ch in d) else ""


def _will_have_value(rv: RegisterValue) -> bool:
    """The register will carry a value after interpretation (digits win; '?' digits leave no value)."""
    return bool(_usable_digits(rv)) or (not rv.digits_seen and rv.value is not None)


def has_reading(res: ExtractionResult) -> bool:
    """A read worth verifying: at least one register with readable digits (or a value)."""
    return any(_will_have_value(r) for m in res.meters for r in m.registers)


def _shown(rv: RegisterValue) -> str:
    return rv.digits_seen or ("" if rv.value is None else str(rv.value))


def same_reading(a: RegisterValue, b: RegisterValue) -> bool:
    """Do two reads of one register show the same number?

    Digits are compared without leading zeros and without the decimal point (the value is computed from the
    digits plus the meter's decimals after matching); when both reads put a point, it must be at the same
    place. A side without usable digits falls back to the model's values.
    """
    da, db = _usable_digits(a), _usable_digits(b)
    if da and db:
        wa, pa, fa = da.partition(".")
        wb, pb, fb = db.partition(".")
        if (wa + fa).lstrip("0") != (wb + fb).lstrip("0"):
            return False
        return not (pa and pb) or len(fa) == len(fb)
    if a.value is not None and b.value is not None and not (da or db):
        return a.value == b.value
    return False


def pair_meters(first: list[MeterReadout], second: list[MeterReadout]) -> dict[int, tuple[int, str]]:
    """Which meter of the second read is which meter of the first: {first index: (second index, how)}.

    1. the same serial (normalized) - ``serial``;
    2. the meters left over are paired in order when both reads left the same number of them (``only`` when
       one each - e.g. a serial misread in one of the reads, ``position`` otherwise);
    otherwise a leftover meter has no counterpart and none of its values counts as reproduced.
    """
    pairs: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    for i, m in enumerate(first):
        ns = normalize_serial(m.serial)
        if not ns:
            continue
        j = next((j for j, o in enumerate(second) if j not in used and normalize_serial(o.serial) == ns), None)
        if j is not None:
            pairs[i] = (j, "serial")
            used.add(j)
    rest_a = [i for i in range(len(first)) if i not in pairs]
    rest_b = [j for j in range(len(second)) if j not in used]
    if rest_a and len(rest_a) == len(rest_b):
        how = "only" if len(rest_a) == 1 else "position"
        pairs.update((i, (j, how)) for i, j in zip(rest_a, rest_b, strict=True))
    return pairs


def compare_reads(first: ExtractionResult, second: ExtractionResult) -> list[dict[str, Any]]:
    """Mark the values of ``first`` that ``second`` (an independent read of the same image) does not reproduce.

    A register value counts as reproduced when the paired meter (``pair_meters``) of the second read shows the
    same number (``same_reading``) for the same register - or for a register either read left unlabelled
    (T1/T2 unknown). Not reproduced (incl. a failed second request): confidence capped at
    ``DOUBT_CONFIDENCE``, flag ``low_confidence`` (a review doubt: never bulk-confirmed, needs an
    acknowledgement) and ``second_read`` = what the second read showed instead. Values are never changed.
    Returns one comparison per checked register (stored in ``raw["verify"]["registers"]``).
    """
    pairs = pair_meters(first.meters, second.meters) if second.ok else {}
    out: list[dict[str, Any]] = []
    for i, m in enumerate(first.meters):
        j, how = pairs.get(i, (None, None))
        other = second.meters[j] if j is not None else None
        for r in m.registers:
            if not _will_have_value(r):
                continue  # nothing will be prefilled from it
            cands = [
                o
                for o in (other.registers if other else [])
                if r.register is None or o.register is None or o.register == r.register
            ]
            agreed = any(same_reading(r, o) for o in cands)
            seen = [s for o in cands if (s := _shown(o))]
            if not agreed:
                r.confidence = min(r.confidence, DOUBT_CONFIDENCE)
                if "low_confidence" not in r.problems:
                    r.problems.append("low_confidence")
                r.second_read = " / ".join(seen)
            out.append(
                {
                    "serial": m.serial,
                    "register": r.register,
                    "first": _shown(r),
                    "second": seen,
                    "paired_by": how,
                    "agreed": agreed,
                }
            )
    return out


def sum_usage(a: Any, b: Any) -> Any:
    """Add two ``usage`` objects of chat/completions answers field by field (tokens, ``cost``, nested details)."""
    if not isinstance(a, dict):
        return b if isinstance(b, dict) else a
    if not isinstance(b, dict):
        return a
    out = dict(a)
    for k, v in b.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = sum_usage(cur, v)
        elif isinstance(cur, int | float) and not isinstance(cur, bool) and isinstance(v, int | float):
            out[k] = cur + v
        elif cur is None:
            out[k] = v
    return out


def make_extractor(settings: Any) -> ReadingExtractor:
    kind = getattr(settings, "extractor", "none")
    if kind == "openai_compat":
        return OpenAICompatExtractor(
            settings.extractor_base_url,
            settings.extractor_model,
            settings.extractor_api_key,
            timeout=float(getattr(settings, "extractor_timeout", 120.0)),
            verify=bool(getattr(settings, "extractor_verify", False)),
        )
    if kind == "fake":
        return FakeExtractor()
    return NoneExtractor()
