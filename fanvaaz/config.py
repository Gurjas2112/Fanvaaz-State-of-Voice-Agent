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

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FANVAAZ_", extra="ignore")

    # What to track: a template id (see fanvaaz/data/profiles) or a path to a profile JSON.
    # Empty = nothing yet: an admin chooses on first sign-in, and that choice is saved in the database.
    profile: str = ""

    # Data. FANVAAZ_DATABASE_URL (postgresql://...) wins over the SQLite file.
    database_url: str | None = None
    db_path: Path = Field(default=ROOT / "data" / "fanvaaz.db")
    raw_dir: Path = Field(default=ROOT / "data" / "raw")
    data_dir: Path = Field(default=ROOT / "fanvaaz" / "data")
    checkpoint_path: Path = Field(default=ROOT / "data" / "checkpoints.db")
    # Keep raw YouTube responses on disk for replay (off on Cloud Run: the disk is ephemeral).
    save_raw: bool = True

    # Collection (N is justified in the README: one search page = 50 results)
    youtube_api_key: str | None = None
    top_n: int = Field(default=50, ge=1, le=50)
    comments_per_video: int = Field(default=100, ge=0, le=100)
    region_code: str = "IN"

    # Metric weights: SoV = a*M + b*E + c*P
    w_mentions: float = 0.3
    w_engagement: float = 0.4
    w_positive: float = 0.3

    # Optional LLM: any OpenAI-compatible endpoint. Default model is Gemini Flash through
    # Google's OpenAI-compatible API (set FANVAAZ_LLM_BASE_URL to GEMINI_OPENAI_BASE).
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gemini-flash-latest"
    llm_timeout_s: float = 45.0
    # USD per 1M tokens, for the cost ledger. Check the provider's price page; override per deploy.
    llm_price_in_per_m: float = 0.30
    llm_price_out_per_m: float = 2.50
    llm_price_cached_per_m: float = 0.075

    # Embeddings for the RAG vector leg (same provider and key as the LLM).
    embed_model: str = "gemini-embedding-001"
    embed_dims: int = Field(default=768, ge=64, le=3072)
    # Optional second-stage reranker: Vertex AI Ranking API (needs GCP project + ADC). Off by default.
    vertex_rerank: bool = False
    gcp_project: str | None = None

    # Governance: the policy file, plus caps that can be tightened per deploy.
    policy_path: Path = Field(default=ROOT / "fanvaaz" / "data" / "policy.yaml")
    llm_enabled: bool = True  # kill switch: false = rules only, everywhere
    max_cost_per_run_usd: float = 0.50
    max_cost_per_day_usd: float = 2.00
    max_prompt_tokens: int = 12_000

    # Memory: /ask follow-ups and saved preferences expire.
    memory_turns: int = Field(default=6, ge=0, le=20)
    memory_ttl_hours: int = Field(default=24, ge=1, le=720)
    prefs_ttl_days: int = Field(default=30, ge=1, le=365)
    ask_rate_per_min: int = Field(default=20, ge=1, le=600)

    # Accounts. The first admin is created at startup from these two values.
    admin_email: str | None = None
    admin_password: str | None = None
    # Comma-separated email domains allowed to sign up (empty = anyone), e.g. "yourbrand.com"
    signup_domains: str = ""
    session_days: int = Field(default=14, ge=1, le=90)
    cookie_secure: bool = False  # set true behind HTTPS
    # Service token for CI and automation (Authorization: Bearer ...); acts as an admin.
    api_token: str | None = None
    # Read-only token for MCP clients (tools that read data; collection still needs api_token).
    mcp_token: str | None = None
    # Serve /docs and /openapi.json to anyone (local dev). In production only admins see them.
    public_docs: bool = True
    # The landing page shows the latest overall share of voice (aggregates only) to visitors.
    public_preview: bool = True
    log_json: bool = False

    # Optional in-process daily collection, local time in `timezone`, e.g. "06:30". Empty = off.
    # On Cloud Run, Cloud Scheduler calls POST /api/v1/runs instead (the instance sleeps when idle).
    daily_run_at: str = ""
    timezone: str = "Asia/Kolkata"

    # Android app (Capacitor/TWA) digital asset link: "package:sha256" (optional).
    android_asset_link: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)

    @property
    def llm_active(self) -> bool:
        return self.llm_configured and self.llm_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
