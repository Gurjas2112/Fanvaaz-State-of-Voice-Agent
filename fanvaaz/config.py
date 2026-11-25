"""Process settings. Secrets come only from the environment (or a git-ignored .env).

What is tracked (brands, aliases, keywords, aspects) is not here: it lives in a
tracking profile (see profile.py), so the same app serves any brand.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FANVAAZ_", extra="ignore")

    # What to track: a preset id (see fanvaaz/data/profiles) or a path to a profile JSON.
    # An admin can switch it later in the admin panel; that choice is saved in the database.
    profile: str = "atomberg-fans"

    # Data
    db_path: Path = Field(default=ROOT / "data" / "fanvaaz.db")
    raw_dir: Path = Field(default=ROOT / "data" / "raw")
    data_dir: Path = Field(default=ROOT / "fanvaaz" / "data")

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
    # Comma-separated email domains allowed to sign up (empty = anyone), e.g. "yourbrand.com"
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
