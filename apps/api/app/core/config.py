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
    xlsx_max_uncompressed_bytes: int = 60 * 1024 * 1024
    xlsx_max_compression_ratio: int = 200

    ai_provider: Literal["disabled", "anthropic"] = "disabled"
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-5-5"
    ai_max_input_chars: int = 20_000
    ai_timeout_seconds: int = 30

    demo_password: str | None = None

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
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env not in ("development", "test")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.secret_key.startswith("dev-only"):
        raise RuntimeError("SECRET_KEY must be set in production")
    return settings
