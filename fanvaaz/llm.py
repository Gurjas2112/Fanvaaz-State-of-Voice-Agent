"""Thin client for any OpenAI-compatible chat endpoint (Groq, Gemini, OpenAI, Ollama).

The LLM is always optional. Every caller has a deterministic fallback, and every
LLM output is validated before use; a failure is logged and degrades quietly.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .config import Settings, get_settings

log = logging.getLogger("fanvaaz.llm")


class LLM(Protocol):
    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


@dataclass
class LLMStats:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: list[float] = field(default_factory=list)


class OpenAICompatLLM:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        s = settings or get_settings()
        if not (s.llm_base_url and s.llm_api_key):
            raise RuntimeError("LLM not configured")
        self.model = s.llm_model
        self.url = s.llm_base_url.rstrip("/") + "/chat/completions"
        self.client = client or httpx.Client(
            timeout=s.llm_timeout_s, headers={"Authorization": f"Bearer {s.llm_api_key}"}
        )
        self.stats = LLMStats()

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        last_err: Exception | None = None
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                r = self.client.post(self.url, json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                data = r.json()
                self.stats.calls += 1
                self.stats.latency_ms.append((time.perf_counter() - t0) * 1000)
                usage = data.get("usage") or {}
                self.stats.prompt_tokens += usage.get("prompt_tokens", 0)
                self.stats.completion_tokens += usage.get("completion_tokens", 0)
                return data["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last_err = e
                time.sleep(0.5 * 2**attempt)
        self.stats.failures += 1
        raise RuntimeError(f"LLM call failed after retries: {last_err}")


def get_llm(settings: Settings | None = None) -> LLM | None:
    s = settings or get_settings()
    if not (s.llm_base_url and s.llm_api_key):
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
