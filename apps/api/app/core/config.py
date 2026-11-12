"""Application settings. All secrets come from the environment; defaults are development-only."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_name: str = "QuotePilot AI"
    secret_key: str = Field(default="dev-only-change-me-dev-only-change-me", min_length=32)
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot"
    database_admin_url: str = "postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot"

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]  # comma-separated in env
    cookie_secure: bool | None = None  # None → derived from app_env
    session_cookie_name: str = "qp_session"
    csrf_cookie_name: str = "qp_csrf"
    session_idle_seconds: int = 12 * 3600
    session_absolute_seconds: int = 7 * 24 * 3600
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 300
    min_password_length: int = 12
    # per-user, in-process (see KNOWN_LIMITATIONS): RFQ creation/re-parse and catalog imports are expensive
    rfq_parse_rate_limit_attempts: int = Field(default=30, ge=1)
    catalog_import_rate_limit_attempts: int = Field(default=10, ge=1)
    expensive_rate_limit_window_seconds: int = Field(default=300, ge=1)

    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = "./var/storage"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None

    max_upload_bytes: int = 10 * 1024 * 1024
    max_pasted_text_chars: int = 100_000
    max_catalog_rows: int = 50_000
    max_rfq_rows: int = 2_000
    max_pdf_pages: int = 50
    parse_timeout_seconds: int = 20
    parser_memory_limit_bytes: int = 768 * 1024 * 1024
    match_timeout_seconds: float = Field(default=8.0, gt=0, le=120)  # wall clock for one batch of fuzzy matching
    xlsx_max_uncompressed_bytes: int = 60 * 1024 * 1024
    xlsx_max_compression_ratio: int = 200

    ai_provider: Literal["disabled", "anthropic"] = "disabled"
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-5-5"
    ai_max_input_chars: int = 20_000
    ai_timeout_seconds: int = 30

    demo_password: str | None = None

    # Stage 2 — follow-up assistant
    token_encryption_key: str | None = None  # base64 32-byte key for provider secrets (AES-256-GCM)
    mail_provider_default: Literal["simulator", "smtp"] = "simulator"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_email: str | None = None
    followup_worker_id: str = "worker-1"
    # Stage 3
    stock_stale_after_hours: int = 24
    retention_grace_days: int = 30
    max_import_rows: int = 10_000
    max_sync_records: int = 5_000
    # Stage 4
    platform_kill_switch: bool = False  # platform-wide: stops assistant turns and workflow steps everywhere
    assistant_max_steps: int = 8  # tool steps per user message
    assistant_max_turns: int = 50  # messages per session
    workflow_max_steps: int = 8  # max workflow depth
    workflow_step_retries: int = 2
    workflow_step_timeout_seconds: int = 20
    circuit_breaker_failures: int = 5  # consecutive failures per org before the breaker opens
    circuit_breaker_cooldown_seconds: int = 300
    default_plan_code: str = "starter"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def dev_endpoints_enabled(self) -> bool:
        """Simulator inbound injection and the in-request worker exist for development and tests only."""
        return self.app_env in ("development", "test")

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env not in ("development", "test")


_WEAK_SECRET_MARKERS = ("dev-only", "change-me", "changeme", "secret-key-test", "e2e-only", "ci-only")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.app_env in ("staging", "production"):
        key = settings.secret_key
        repeated = len(key) >= 8 and any(
            key == key[:n] * (len(key) // n) for n in range(1, len(key) // 2 + 1) if len(key) % n == 0
        )
        weak = any(m in key.lower() for m in _WEAK_SECRET_MARKERS) or len(key) < 32 or len(set(key)) < 16 or repeated
        if weak:
            raise RuntimeError("SECRET_KEY is a placeholder or low-entropy value; set a random 64-character secret")
    return settings
