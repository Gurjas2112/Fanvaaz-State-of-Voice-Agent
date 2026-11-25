"""Token counting before and after a call.

Before: a cheap local estimate gates every call against the context budget and
the cost cap, without a network round trip. After: the provider's `usage` is
recorded next to the estimate, so the AI Ops panel shows how far off the
estimate runs (and the estimator can be recalibrated from real numbers).

Calibration: about 4 characters per token for English, about 2 for Devanagari.
For an exact count on Gemini, `count_tokens_gemini` calls the native countTokens
endpoint (used by `fanvaaz tokens`, not on the hot path).
"""

from __future__ import annotations

import math

import httpx

CHARS_PER_TOKEN_LATIN = 4.0
CHARS_PER_TOKEN_OTHER = 2.0
PER_MESSAGE_OVERHEAD = 4


def estimate(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return math.ceil(ascii_chars / CHARS_PER_TOKEN_LATIN + other / CHARS_PER_TOKEN_OTHER)


def estimate_messages(system: str, user: str) -> int:
    return estimate(system) + estimate(user) + 2 * PER_MESSAGE_OVERHEAD


def fit(texts: list[str], budget: int) -> tuple[list[str], int]:
    """Keep texts in order until the token budget runs out; returns (kept, tokens used)."""
    kept, used = [], 0
    for t in texts:
        n = estimate(t)
        if used + n > budget:
            break
        kept.append(t)
        used += n
    return kept, used


def cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    price_in: float,
    price_out: float,
    price_cached: float,
) -> float:
    """Cost of one call. Cached prompt tokens are billed at the cached rate."""
    fresh = max(prompt_tokens - cached_tokens, 0)
    return (fresh * price_in + cached_tokens * price_cached + completion_tokens * price_out) / 1_000_000


def count_tokens_gemini(api_key: str, model: str, text: str, timeout: float = 15.0) -> int:
    """Exact count from Gemini's native countTokens endpoint."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens"
    r = httpx.post(
        url,
        headers={"x-goog-api-key": api_key},
        json={"contents": [{"parts": [{"text": text}]}]},
        timeout=timeout,
    )
    r.raise_for_status()
    return int(r.json().get("totalTokens", 0))
