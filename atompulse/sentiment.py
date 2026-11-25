"""Brand-targeted sentiment for English + Hinglish fan comments.

The rule engine is the always-available baseline and the fallback whenever the
LLM is off, slow or returns something invalid. It handles the cases that break
naive lexicon scoring in this domain:

* targets: "Atomberg is great but Havells is noisy" -> Atomberg +, Havells -
* comparatives: "Atomberg is quieter than Crompton" -> Atomberg +, Crompton -
* switches: "switched from Usha to Atomberg" -> Usha -, Atomberg +
* negation, before and after: "not noisy", "accha nahi hai"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .brands import Span, find_spans
from .config import ASPECTS
from .guardrails import is_injection
from .models import Sentiment

POSITIVE_PHRASES = [
    "value for money",
    "paisa vasool",
    "paisa wasool",
    "worth it",
    "worth the money",
    "low bill",
    "bill reduced",
    "reduced bill",
    "bill came down",
    "highly recommend",
    "go for it",
    "best fan",
    "no noise",
    "no sound",
    "zero noise",
    "बहुत अच्छा",
]
NEGATIVE_PHRASES = [
    "not working",
    "stopped working",
    "waste of money",
    "dont buy",
    "don't buy",
    "do not buy",
    "never buy",
    "worst fan",
    "remote not working",
    "app not working",
    "no response",
    "poor service",
    "costs too much",
    "too costly",
    "too expensive",
    "awaaz aati",
    "aawaz aati",
]
POSITIVE_WORDS = {
    "good",
    "great",
    "best",
    "excellent",
    "awesome",
    "amazing",
    "love",
    "loved",
    "loving",
    "superb",
    "nice",
    "silent",
    "quiet",
    "quieter",
    "recommend",
    "recommended",
    "worth",
    "accha",
    "achha",
    "acha",
    "achcha",
    "badhiya",
    "badiya",
    "mast",
    "zabardast",
    "jabardast",
    "shandar",
    "perfect",
    "happy",
    "satisfied",
    "saves",
    "saving",
    "saved",
    "efficient",
    "smooth",
    "premium",
    "reliable",
    "sturdy",
    "beautiful",
    "fantastic",
    "solid",
    "impressed",
    "khush",
    "better",
    "superior",
    "cheaper",
    "top",
    "brilliant",
    "elegant",
    "stylish",
    "fast",
    "quick",
    "helpful",
    "durable",
    "wonderful",
    "fine",
    "अच्छा",
    "अच्छी",
    "बढ़िया",
    "शानदार",
    "बेहतरीन",
}
NEGATIVE_WORDS = {
    "bad",
    "worst",
    "poor",
    "terrible",
    "horrible",
    "waste",
    "useless",
    "noisy",
    "noise",
    "defective",
    "broke",
    "broken",
    "issue",
    "issues",
    "problem",
    "problems",
    "stopped",
    "faulty",
    "disappointed",
    "disappointing",
    "regret",
    "pathetic",
    "bakwas",
    "bekar",
    "bekaar",
    "kharab",
    "ghatiya",
    "wobble",
    "wobbling",
    "expensive",
    "costly",
    "overpriced",
    "delay",
    "delayed",
    "dead",
    "failed",
    "fail",
    "fails",
    "complaint",
    "avoid",
    "scam",
    "fraud",
    "humming",
    "hum",
    "mehenga",
    "refund",
    "worse",
    "louder",
    "slower",
    "rude",
    "lag",
    "laggy",
    "disconnects",
    "disconnect",
    "heating",
    "sparking",
    "slow",
    "buggy",
    "overrated",
    "awaaz",
    "aawaz",
    "बेकार",
    "खराब",
    "बकवास",
    "घटिया",
}
NEGATORS = {
    "not",
    "no",
    "never",
    "dont",
    "don't",
    "didnt",
    "didn't",
    "isnt",
    "isn't",
    "wasnt",
    "wasn't",
    "hardly",
    "without",
    "nor",
    "doesnt",
    "doesn't",
    "cannot",
    "cant",
    "can't",
}
POST_NEGATORS = {"nahi", "nahin", "nai", "nhi", "na"}

# A comparative word between two brands decides both sides at once.
COMPARE_POS = {"better", "superior", "quieter", "cheaper", "faster", "smoother", "best"}
COMPARE_NEG = {"worse", "louder", "noisier", "costlier", "slower", "inferior"}

CLAUSE_SPLIT = re.compile(r"[.!?;\n]+|\bbut\b|\bhowever\b|\blekin\b|\bmagar\b|\bwhereas\b|\bkintu\b", re.I)
TOKEN_RE = re.compile(r"[a-z']+|[ऀ-ॿ]+")
SWITCH_RE = re.compile(r"\b(?:switched|moved|shifted|changed|upgraded)\s+from\b", re.I)
REPLACE_RE = re.compile(r"\breplaced\b", re.I)


@dataclass
class BrandSentiment:
    sentiment: Sentiment
    aspects: list[str] = field(default_factory=list)
    score: float = 0.0


def _polarity(text: str) -> float:
    low = text.lower()
    score = 0.0
    for p in POSITIVE_PHRASES:
        if p in low:
            score += 1.5
            low = low.replace(p, " ")
    for p in NEGATIVE_PHRASES:
        if p in low:
            score -= 1.5
            low = low.replace(p, " ")
    toks = TOKEN_RE.findall(low)
    for i, t in enumerate(toks):
        val = 1.0 if t in POSITIVE_WORDS else -1.0 if t in NEGATIVE_WORDS else 0.0
        if not val:
            continue
        before = toks[max(0, i - 3) : i]
        after = toks[i + 1 : i + 3]
        if any(b in NEGATORS for b in before) or any(a in POST_NEGATORS for a in after):
            val = -val * 0.8
        score += val
    return score


def _label(score: float) -> Sentiment:
    if score > 0.25:
        return "positive"
    if score < -0.25:
        return "negative"
    return "neutral"


def aspects_of(text: str) -> list[str]:
    low = text.lower()
    found = []
    for aspect, words in ASPECTS.items():
        if any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", low) for w in words):
            found.append(aspect)
    return found


def _clauses(text: str) -> list[tuple[int, int, str, bool]]:
    """(start, end, clause, follows_contrast): clauses after "but"/"lekin" weigh more."""
    out, start, contrast = [], 0, False
    for m in CLAUSE_SPLIT.finditer(text):
        if m.start() > start:
            out.append((start, m.start(), text[start : m.start()], contrast))
        contrast = bool(re.match(r"[a-z]", m.group(0), re.I))
        start = m.end()
    if start < len(text):
        out.append((start, len(text), text[start:], contrast))
    return out


def _comparative(clause: str, spans: list[Span]) -> dict[str, float] | None:
    """Handle 'A ... better/worse than B' and 'from B to A' inside one clause."""
    brands = []
    for sp in spans:
        if sp.brand not in brands:
            brands.append(sp.brand)
    if len(brands) < 2:
        return None
    a, b = brands[0], brands[1]
    low = clause.lower()
    if SWITCH_RE.search(clause):  # "switched from A to B": A loses, B wins
        return {a: -1.0, b: 1.0}
    if REPLACE_RE.search(clause):  # "replaced A with B"
        return {a: -1.0, b: 1.0}
    if " than " in f" {low} ":
        toks = set(TOKEN_RE.findall(low))
        if toks & COMPARE_POS:
            return {a: 1.0, b: -1.0}
        if toks & COMPARE_NEG:
            return {a: -1.0, b: 1.0}
    return None


CHOICE_RE = re.compile(r"\b(or|vs\.?|versus|ya)\b|\bwhich\b|\bkaunsa\b|\bkonsa\b", re.I)


def _is_choice_question(text: str, clause: str, end: int) -> bool:
    is_question = text[end : end + 1] == "?" or clause.strip().lower().startswith(
        ("which", "should", "kaunsa", "konsa")
    )
    return is_question and bool(CHOICE_RE.search(clause))


def score_text(text: str) -> dict[str, BrandSentiment]:
    """Sentiment toward every brand mentioned in `text`."""
    spans = find_spans(text)
    if not spans:
        return {}
    distinct = {sp.brand for sp in spans}
    # An instruction aimed at the model is not an opinion: it counts as a mention, never as praise.
    if is_injection(text):
        return {b: BrandSentiment("neutral", []) for b in distinct}
    result: dict[str, BrandSentiment] = {}
    clauses = _clauses(text)
    # Whole-comment tone, with the clause after "but" weighted higher ("silent but overpriced").
    whole = sum(
        0.0 if _is_choice_question(text, c, e) else _polarity(c) * (1.5 if after_but else 1.0)
        for _, e, c, after_but in clauses
    )
    for start, end, clause, _ in clauses:
        in_clause = [sp for sp in spans if start <= sp.start < end]
        if not in_clause:
            continue
        if _is_choice_question(text, clause, end):
            # "Which is better, Atomberg or Havells?" asks; it does not judge.
            comp, clause_score = None, 0.0
        else:
            comp = _comparative(clause, in_clause)
            clause_score = _polarity(clause)
        for sp in in_clause:
            s = comp[sp.brand] if comp and sp.brand in comp else clause_score
            prev = result.get(sp.brand)
            aspects = aspects_of(clause)
            if prev is None:
                result[sp.brand] = BrandSentiment(_label(s), aspects, s)
            else:
                prev.score += s
                prev.sentiment = _label(prev.score)
                prev.aspects = sorted(set(prev.aspects) | set(aspects))
    # With one brand, the comment's overall tone is the brand's tone:
    # "Bought Atomberg last month. Totally worth it!", "Stealth is silent but overpriced".
    if len(distinct) == 1:
        (brand,) = distinct
        bs = result[brand]
        if abs(whole) > 0.25:
            bs.score = whole
            bs.sentiment = _label(whole)
            bs.aspects = aspects_of(text)
    return result
