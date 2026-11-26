"""Tracking profiles: which brand to report on, against whom, and on which searches.

Fanvaaz is not tied to one company. A profile names the focus brand, its rivals
and their aliases, the YouTube searches buyers use, the product aspects worth
explaining, and any extra risk phrases for the category. Nothing is tracked by
default: on first sign-in an admin picks an example template (smartphones, EV
scooters) or pastes their own profile, and the next run uses it.

Every run stores the profile it used, so changing the profile never rewrites
history: old runs keep their brands, and "since the last run" only compares runs
that tracked the same focus brand.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRESET_DIR = Path(__file__).resolve().parent / "data" / "profiles"

# One keyword costs about 151 YouTube quota units (search 100 + videos 1 +
# 50 comment threads); the free daily quota is 10,000. Ten keywords stay well inside it.
MAX_KEYWORDS = 10
MAX_BRANDS = 20
RISK_CATEGORY_NAMES = ("safety", "legal_consumer", "product_failure", "service_failure")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def _clean_terms(values: list[str], what: str, max_items: int, max_len: int = 40) -> list[str]:
    out: list[str] = []
    for v in values:
        t = " ".join(v.split())
        if not t:
            continue
        if len(t) > max_len:
            raise ValueError(f"{what} '{t[:20]}…' is longer than {max_len} characters")
        if t.lower() not in {o.lower() for o in out}:
            out.append(t)
    if len(out) > max_items:
        raise ValueError(f"at most {max_items} {what}s")
    return out


class NoProfileError(RuntimeError):
    """Nothing is tracked yet: an admin must choose or write a profile first."""


class Profile(BaseModel):
    """What one Fanvaaz workspace tracks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(description="Short slug, e.g. 'smartphones-india'")
    name: str = Field(min_length=2, max_length=60, description="Shown to users, e.g. 'Smartphones (India)'")
    category: str = Field(min_length=2, max_length=40, description="Plural product noun, e.g. 'smartphones'")
    focus_brand: str = Field(min_length=1, max_length=40)
    brands: dict[str, list[str]] = Field(description="Brand -> lowercase aliases matched on word boundaries")
    keywords: list[str] = Field(description="YouTube searches, as buyers type them")
    aspects: dict[str, list[str]] = Field(default_factory=dict, description="Aspect -> trigger words")
    playbook: dict[str, str] = Field(default_factory=dict, description="Aspect -> suggested action")
    risk_terms: dict[str, list[str]] = Field(
        default_factory=dict, description="Extra risk phrases per category for this product"
    )
    domain_words: list[str] = Field(
        default_factory=list, description="Extra words that keep 'Ask' questions on topic"
    )

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError("id must be 2-40 lowercase letters, digits or hyphens")
        return v

    @field_validator("brands")
    @classmethod
    def _brands(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if not 2 <= len(v) <= MAX_BRANDS:
            raise ValueError(f"track between 2 and {MAX_BRANDS} brands (the focus brand and its rivals)")
        out: dict[str, list[str]] = {}
        owner: dict[str, str] = {}
        for brand, aliases in v.items():
            b = " ".join(brand.split())
            if not 1 <= len(b) <= 40:
                raise ValueError("brand names must be 1-40 characters")
            terms = [t.lower() for t in _clean_terms([b, *aliases], "alias", 15)]
            for t in terms:
                if len(t) < 2:
                    raise ValueError(f"alias '{t}' is too short to match reliably")
                if t in owner and owner[t] != b:
                    raise ValueError(f"alias '{t}' is listed for both {owner[t]} and {b}")
                owner[t] = b
            out[b] = terms
        return out

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, v: list[str]) -> list[str]:
        out = _clean_terms(v, "keyword", MAX_KEYWORDS, max_len=60)
        if not out:
            raise ValueError("add at least one YouTube search keyword")
        return out

    @field_validator("aspects")
    @classmethod
    def _aspects(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if len(v) > 12:
            raise ValueError("at most 12 aspects")
        return {
            " ".join(a.split()).lower(): [w.lower() for w in _clean_terms(ws, "aspect word", 30)]
            for a, ws in v.items()
        }

    @field_validator("risk_terms")
    @classmethod
    def _risk(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        bad = set(v) - set(RISK_CATEGORY_NAMES)
        if bad:
            raise ValueError(f"risk categories must be among {', '.join(RISK_CATEGORY_NAMES)}")
        return {k: [w.lower() for w in _clean_terms(ws, "risk term", 40)] for k, ws in v.items()}

    @field_validator("domain_words")
    @classmethod
    def _domain(cls, v: list[str]) -> list[str]:
        return [w.lower() for w in _clean_terms(v, "domain word", 80)]

    @model_validator(mode="after")
    def _focus_known(self) -> Profile:
        if self.focus_brand not in self.brands:
            raise ValueError(f"focus_brand '{self.focus_brand}' must be one of the tracked brands")
        unknown = set(self.playbook) - set(self.aspects)
        if unknown:
            raise ValueError(f"playbook entries need a matching aspect: {', '.join(sorted(unknown))}")
        return self

    @property
    def rivals(self) -> list[str]:
        return [b for b in self.brands if b != self.focus_brand]


def presets() -> dict[str, Profile]:
    out = {}
    for p in sorted(PRESET_DIR.glob("*.json")):
        prof = Profile.model_validate(json.loads(p.read_text(encoding="utf-8")))
        out[prof.id] = prof
    return out


def load(ref: str) -> Profile:
    """A preset id, or a path to a profile JSON file."""
    path = Path(ref)
    if path.suffix == ".json" and path.exists():
        return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    all_presets = presets()
    if ref not in all_presets:
        raise ValueError(f"unknown profile '{ref}'; presets: {', '.join(all_presets)}")
    return all_presets[ref]


# ---- the active profile -----------------------------------------------------
# A workspace can track several profiles side by side (smartphones and EV
# scooters, say). Modules read the current one through active():
#
#   * `use(p)` scopes a profile to the current context: one HTTP request (set by
#     middleware), one collection run, one thread. Concurrent requests for
#     different profiles never see each other's profile.
#   * outside any scope, active() falls back to the process default (the
#     workspace's default profile, or FANVAAZ_PROFILE).
#
# Caches derived from a profile (compiled brand patterns, topic words) are keyed
# by `fingerprint(p)`, so two profiles can be cached at once; the reset hooks
# remain for tests that replace the default.

_lock = threading.Lock()
_active: Profile | None = None
_hooks: list[Callable[[], None]] = []
_scoped: contextvars.ContextVar[Profile | None] = contextvars.ContextVar("fanvaaz_profile", default=None)


_fp_memo: dict[int, tuple[Profile, str]] = {}


def fingerprint(p: Profile) -> str:
    """A stable key for caches: the same profile content gives the same key (memoised per object)."""
    hit = _fp_memo.get(id(p))
    if hit is not None and hit[0] is p:
        return hit[1]
    fp = p.id + ":" + hashlib.sha256(p.model_dump_json().encode("utf-8")).hexdigest()[:12]
    if len(_fp_memo) > 256:
        _fp_memo.clear()
    _fp_memo[id(p)] = (p, fp)
    return fp


def per_profile(build: Callable[[Profile], object]):
    """Cache `build(active())` per profile fingerprint; `.cache_clear()` empties it."""
    cache: dict[str, object] = {}

    def get():
        p = active()
        key = fingerprint(p)
        val = cache.get(key)
        if val is None:
            if len(cache) > 16:
                cache.clear()
            val = cache[key] = build(p)
        return val

    get.cache_clear = cache.clear  # type: ignore[attr-defined]
    return get


@contextmanager
def use(p: Profile):
    """Make `p` the active profile for this context (request, run or thread)."""
    token = _scoped.set(p)
    try:
        yield p
    finally:
        _scoped.reset(token)


def on_change(fn: Callable[[], None]) -> Callable[[], None]:
    _hooks.append(fn)
    return fn


def configured() -> bool:
    """True once a profile is active or one is set in FANVAAZ_PROFILE."""
    from .config import get_settings

    return _scoped.get() is not None or _active is not None or bool(get_settings().profile)


def active() -> Profile:
    global _active
    scoped = _scoped.get()
    if scoped is not None:
        return scoped
    if _active is None:
        with _lock:
            if _active is None:
                from .config import get_settings

                ref = get_settings().profile
                if not ref:
                    raise NoProfileError(
                        "Nothing is tracked yet. An admin must choose what to track on the admin page."
                    )
                _active = load(ref)
    return _active


def set_active(profile: Profile) -> Profile:
    global _active
    with _lock:
        _active = profile
    for fn in _hooks:
        fn()
    return profile


def reset() -> None:
    """Forget the active profile (tests); the next active() reloads from settings."""
    global _active
    with _lock:
        _active = None
    for fn in _hooks:
        fn()


def scoped() -> Profile | None:
    """The profile set by use() for this context, if any."""
    return _scoped.get()


def resolve(store, profile_id: str | None) -> Profile:
    """A saved profile by id; an unknown or missing id gives the workspace default."""
    if profile_id:
        saved = store.saved_profile(profile_id)
        if saved:
            try:
                return Profile.model_validate(saved["profile"])
            except ValueError:
                pass
    return sync_from(store)


def sync_from(store) -> Profile:
    """Use the profile an admin saved, if any; otherwise the configured preset.

    Raises NoProfileError when neither exists (a fresh install before onboarding).
    """
    saved = store.saved_profile()
    if saved:
        try:
            prof = Profile.model_validate(saved["profile"])
        except ValueError:  # a saved profile that no longer validates: keep the default
            return active()
        if prof != _active:
            set_active(prof)
        return prof
    return active()
