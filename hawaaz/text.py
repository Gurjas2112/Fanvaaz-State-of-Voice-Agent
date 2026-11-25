"""Text normalisation and PII redaction.

Redaction happens at ingestion, before text is stored, embedded, shown or sent to
an LLM. The safety suite asserts that no stored or returned text matches these
patterns.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Indian mobile numbers: optional +91 / 0 prefix, 10 digits starting 6-9,
# allowing spaces or dashes in common groupings.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

EMAIL_TOKEN = "[email]"
PHONE_TOKEN = "[phone]"


def clean(text: str) -> str:
    """Unescape HTML entities, strip tags, normalise unicode and whitespace."""
    text = html.unescape(text or "")
    text = TAG_RE.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    return WS_RE.sub(" ", text).strip()


def redact_pii(text: str) -> str:
    text = EMAIL_RE.sub(EMAIL_TOKEN, text)
    return PHONE_RE.sub(PHONE_TOKEN, text)


def contains_pii(text: str) -> bool:
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))


def prepare(text: str) -> str:
    """The one entry point ingestion uses: clean, then redact."""
    return redact_pii(clean(text))


def hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
