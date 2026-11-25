"""Input and output guardrails for every LLM-facing path.

Input  : prompt-injection and off-topic detection on user questions; injection
         detection on retrieved comments (they are attacker-controlled text).
Output : PII redaction and numeric grounding: every number an answer states
         must exist in the evidence it was given, or the answer is rejected.

The red-team suite (tests/safety) measures block rate and false-refusal rate
against these functions and fails CI below the thresholds in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brands import brands_in
from .text import contains_pii, redact_pii

INJECTION_PATTERNS = [
    r"\bignore\b.{0,40}\b(instruction|instructions|rules|prompt|above|previous|prior)\b",
    r"\bdisregard\b.{0,40}\b(instruction|instructions|rules|above|previous|prior)\b",
    r"\bforget\b.{0,30}\b(everything|instructions|rules|previous)\b",
    r"\b(system|developer|hidden)\s+(prompt|message|instructions?)\b",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\b",
    r"\bpretend\s+(to\s+be|you)\b",
    r"\b(jailbreak|dan\s+mode|developer\s+mode|god\s+mode)\b",
    r"\b(reveal|print|show|repeat|output|leak)\b.{0,30}\b(prompt|instructions|api\s*key|secret|token|password|env)\b",
    r"\bnew\s+instructions?\b",
    r"\boverride\b.{0,30}\b(rules|instructions|safety|policy)\b",
    r"<\|?(im_start|im_end|system)\|?>",
    r"\bbhool\s+jao\b",
    r"\b(say|declare|write|state)\b.{0,40}\b(is the best|is better|is the worst)\b.{0,20}\b(always|regardless)\b",
    r"\b(phone|mobile|email|contact)\s*(number|numbers|address|id|details)?\b.{0,30}\b(of|for)\b.{0,20}\b(commenter|user|users|people|customer|customers|author)s?\b",
    r"\b(list|give|share|dump|export)\b.{0,30}\b(emails?|phone numbers?|personal|contact details)\b",
]
_INJ = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in INJECTION_PATTERNS]

DOMAIN_WORDS = {
    "fan",
    "fans",
    "brand",
    "brands",
    "sov",
    "share",
    "voice",
    "sentiment",
    "comment",
    "comments",
    "video",
    "videos",
    "youtube",
    "keyword",
    "keywords",
    "marketing",
    "content",
    "review",
    "reviews",
    "noise",
    "remote",
    "bill",
    "energy",
    "bldc",
    "ceiling",
    "smart",
    "app",
    "service",
    "price",
    "competitor",
    "competitors",
    "customers",
    "people",
    "viewers",
    "mention",
    "mentions",
    "positive",
    "negative",
    "complaint",
    "complaints",
    "influencer",
    "channel",
    "channels",
    "engagement",
    "campaign",
    "recommend",
    "recommendation",
    "recommendations",
    "why",
    "insight",
    "insights",
    "atomberg",
    "lead",
    "leads",
    "rank",
    "ranking",
    "top",
    "buyers",
    "say",
    "think",
    "feel",
    "like",
    "dislike",
    "quality",
    "installation",
    "warranty",
    "design",
    "speed",
    "air",
    "saving",
}

REFUSAL = (
    "I can only answer questions about fan brands' share of voice and what YouTube viewers say, "
    "and I don't follow instructions to change that."
)


@dataclass
class Verdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def injection_hits(text: str) -> list[str]:
    return [p.pattern for p in _INJ if p.search(text)]


def is_injection(text: str) -> bool:
    return bool(injection_hits(text))


def is_on_topic(text: str) -> bool:
    toks = set(re.findall(r"[a-z]+", text.lower()))
    return bool(toks & DOMAIN_WORDS) or bool(brands_in(text))


def check_question(question: str) -> Verdict:
    reasons = []
    q = question.strip()
    if not q:
        reasons.append("empty")
    if len(q) > 500:
        reasons.append("too_long")
    if is_injection(q):
        reasons.append("prompt_injection")
    if contains_pii(q):
        reasons.append("pii_in_question")
    if q and not is_on_topic(q):
        reasons.append("off_topic")
    return Verdict(allowed=not reasons, reasons=reasons)


NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%?)")


def numbers_in(text: str) -> list[tuple[float, bool]]:
    return [(float(n), bool(pct)) for n, pct in NUM_RE.findall(text)]


def ungrounded_numbers(
    answer: str, evidence: list[float], pct_tol: float = 0.15, abs_tol: float = 0.01
) -> list[float]:
    """Numbers in `answer` that do not match any evidence value.

    Evidence holds raw values (shares as fractions, counts as ints). A percentage in
    the answer matches a fraction x if |pct - 100x| <= pct_tol. Small integers
    (<= 10) are allowed freely: "top 3", "2 keywords".
    """
    bad = []
    for value, is_pct in numbers_in(answer):
        if not is_pct and value <= 10 and value.is_integer():
            continue
        ok = False
        for ev in evidence:
            if is_pct and (abs(value - 100 * ev) <= pct_tol or abs(value - ev) <= pct_tol):
                ok = True
                break
            if not is_pct and (abs(value - ev) <= abs_tol or abs(value - 100 * ev) <= pct_tol):
                ok = True
                break
        if not ok:
            bad.append(value)
    return bad


def safe_output(text: str) -> str:
    """Last line of defence before anything leaves the API."""
    return redact_pii(text)
