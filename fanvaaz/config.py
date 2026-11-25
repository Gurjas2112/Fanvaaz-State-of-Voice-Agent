"""Settings, tracked brands and keywords.

Everything tunable lives here so the QA harness can assert on it and the README
can quote it. Secrets come only from the environment (or a git-ignored .env).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# The brand we report on, and the competitors it is compared against.
FOCUS_BRAND = "Atomberg"

# Aliases are matched case-insensitively on word boundaries (see brands.py).
# Model names are only listed when they are unambiguous in fan conversations.
BRAND_ALIASES: dict[str, list[str]] = {
    "Atomberg": ["atomberg", "atom berg", "atomburg", "renesa", "efficio", "ameza", "एटमबर्ग"],
    "Havells": ["havells", "havels", "हैवेल्स"],
    "Crompton": ["crompton", "energion", "क्रॉम्पटन"],
    "Orient": ["orient electric", "orient", "aeroquiet"],
    "Usha": ["usha"],
    "Bajaj": ["bajaj"],
    "Polycab": ["polycab"],
    # "super fan" is left out on purpose: "I'm a super fan of Atomberg" is not a Superfan mention.
    "Superfan": ["superfan", "versa drives"],
    "Panasonic": ["panasonic"],
    "Luminous": ["luminous fan", "luminous fans", "luminous"],
}

# Keywords searched on YouTube. "smart fan" is the problem statement's keyword;
# the rest are the adjacent queries Indian buyers actually type.
DEFAULT_KEYWORDS: list[str] = [
    "smart fan",
    "BLDC fan",
    "best ceiling fan India",
    "energy saving fan",
    "ceiling fan with remote",
    "smart ceiling fan app",
]

# Aspect vocabulary used to explain *why* sentiment is positive or negative.
ASPECTS: dict[str, list[str]] = {
    "energy saving": [
        "bill",
        "electricity",
        "power saving",
        "energy",
        "saves",
        "saving",
        "watt",
        "bijli",
        "inverter",
    ],
    "noise": ["noise", "noisy", "silent", "quiet", "quieter", "louder", "sound", "humming", "awaaz"],
    "remote & app": ["remote", "app", "wifi", "wi-fi", "alexa", "google home", "smart"],
    "air delivery": ["air", "speed", "breeze", "hawa", "rpm", "cmm"],
    "service": [
        "service",
        "installation",
        "technician",
        "warranty",
        "customer care",
        "support",
        "replacement",
    ],
    "price": ["price", "cost", "expensive", "cheap", "paisa", "value for money", "costly", "mehenga"],
    "build & design": ["design", "look", "looks", "build", "quality", "finish", "wobble", "colour", "color"],
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FANVAAZ_", extra="ignore")

    # Data
    db_path: Path = Field(default=ROOT / "data" / "fanvaaz.db")
    raw_dir: Path = Field(default=ROOT / "data" / "raw")
    demo_data: Path = Field(default=ROOT / "fanvaaz" / "data" / "demo_raw.json")

    # Collection (N is justified in the README: one search page = 50 results)
    youtube_api_key: str | None = None
    top_n: int = Field(default=50, ge=1, le=50)
    comments_per_video: int = Field(default=100, ge=0, le=100)
    region_code: str = "IN"

    # Metric weights: SoV = a*M + b*E + c*P
    w_mentions: float = 0.3
    w_engagement: float = 0.4
    w_positive: float = 0.3

    # Optional LLM (any OpenAI-compatible endpoint: Groq, Gemini, OpenAI, Ollama)
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    llm_timeout_s: float = 30.0

    # Accounts. The first admin is created at startup from these two values.
    admin_email: str | None = None
    admin_password: str | None = None
    # Comma-separated email domains allowed to sign up (empty = anyone), e.g. "atomberg.com"
    signup_domains: str = ""
    session_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool = False  # set true behind HTTPS
    # Service token for CI and automation (Authorization: Bearer ...); acts as an admin.
    api_token: str | None = None

    # Optional daily collection, local time in `timezone`, e.g. "06:30". Empty = off.
    daily_run_at: str = ""
    daily_run_source: str = "youtube"
    timezone: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()
