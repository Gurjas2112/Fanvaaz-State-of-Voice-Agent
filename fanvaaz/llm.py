"""Thin client for any OpenAI-compatible endpoint (Gemini, Groq, OpenAI, Ollama).

The LLM is always optional. Every caller has a deterministic fallback, and every
call goes through the gateway in ai.py (governance, budget, cache, schema check,
ledger). This module only speaks HTTP.

Gemini is used through Google's OpenAI-compatible API:
  FANVAAZ_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
  FANVAAZ_LLM_MODEL=gemini-flash-latest
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import Settings, get_settings

log = logging.getLogger("fanvaaz.llm")


class LLM(Protocol):
    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


@dataclass
class Reply:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: float = 0.0
    usage_reported: bool = False


@dataclass
class LLMStats:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: list[float] = field(default_factory=list)


RETRYABLE = (429, 500, 502, 503, 504)


class OpenAICompatLLM:
    cacheable = True  # temperature 0: the same prompt on the same input may be served from cache

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        s = settings or get_settings()
        if not s.llm_configured:
            raise RuntimeError("LLM not configured")
        self.s = s
        self.model = s.llm_model
        self.base = s.llm_base_url.rstrip("/")
        self.url = self.base + "/chat/completions"
        self.client = client or httpx.Client(
            timeout=s.llm_timeout_s, headers={"Authorization": f"Bearer {s.llm_api_key}"}
        )
        self.stats = LLMStats()
        self._schema_supported = True
        # Circuit breaker: an auth or billing failure (401/402/403) will not fix itself mid-run,
        # so the client stops calling the provider and every later call is refused at once.
        self.circuit_open: str | None = None

    # ---- chat -------------------------------------------------------------------
    def _post(self, url: str, body: dict) -> dict:
        if self.circuit_open:
            raise RuntimeError(f"LLM circuit open: {self.circuit_open}")
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = self.client.post(url, json=body)
                if r.status_code in RETRYABLE:
                    raise httpx.HTTPStatusError(f"retryable {r.status_code}", request=r.request, response=r)
                if r.status_code in (401, 402, 403):
                    try:
                        detail = (r.json()[0] if isinstance(r.json(), list) else r.json())["error"]["message"]
                    except Exception:  # noqa: BLE001
                        detail = r.text[:200]
                    self.circuit_open = f"{r.status_code} {detail.strip()[:200]}"
                    log.error(
                        "LLM provider refused (%s); LLM steps are off for this process", self.circuit_open
                    )
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response is not None and e.response.status_code not in RETRYABLE:
                    raise
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
            time.sleep(0.5 * 2**attempt)
        raise RuntimeError(f"LLM call failed after retries: {last_err}")

    def complete_ex(
        self, system: str, user: str, *, json_mode: bool = False, json_schema: dict | None = None
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            # System first and unchanged between calls: providers cache the shared prefix.
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_schema is not None and self._schema_supported:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": json_schema},
            }
        elif json_mode or json_schema is not None:
            body["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            data = self._post(self.url, body)
        except httpx.HTTPStatusError as e:
            # Some endpoints reject json_schema; fall back to plain JSON mode once and remember.
            if json_schema is not None and self._schema_supported and e.response.status_code == 400:
                log.warning("endpoint rejected json_schema; falling back to json_object")
                self._schema_supported = False
                return self.complete_ex(system, user, json_mode=True)
            self.stats.failures += 1
            raise RuntimeError(f"LLM call failed: {e}") from e
        except RuntimeError:
            self.stats.failures += 1
            raise
        ms = (time.perf_counter() - t0) * 1000
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            self.stats.failures += 1
            raise RuntimeError("LLM reply had no content") from e
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        rep = Reply(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            latency_ms=ms,
            usage_reported=bool(usage),
        )
        self.stats.calls += 1
        self.stats.latency_ms.append(ms)
        self.stats.prompt_tokens += rep.prompt_tokens
        self.stats.completion_tokens += rep.completion_tokens
        self.stats.cached_tokens += rep.cached_tokens
        return rep

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        return self.complete_ex(system, user, json_mode=json_mode).text

    # ---- embeddings -----------------------------------------------------------------
    def embed(self, texts: list[str], model: str | None = None, dims: int | None = None) -> list[list[float]]:
        """Embeddings, truncated to `dims` and re-normalised (Matryoshka-style models allow this)."""
        model = model or self.s.embed_model
        dims = dims or self.s.embed_dims
        out: list[list[float]] = []
        for i in range(0, len(texts), 100):
            chunk = texts[i : i + 100]
            body: dict[str, Any] = {"model": model, "input": chunk}
            data = self._post(self.base + "/embeddings", body)
            rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            if len(rows) != len(chunk):
                raise RuntimeError("embedding count mismatch")
            for row in rows:
                out.append(normalise(row["embedding"][:dims]))
        return out


def normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def get_llm(settings: Settings | None = None) -> LLM | None:
    s = settings or get_settings()
    if not s.llm_active:
        return None
    try:
        return OpenAICompatLLM(s)
    except RuntimeError:
        return None


def parse_json(text: str) -> Any:
    """Parse a JSON reply, tolerating ```json fences some models add."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{") :] if "{" in t else t
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(t[start : end + 1])
