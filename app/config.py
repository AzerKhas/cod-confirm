"""Application settings, loaded from environment variables / `.env`.

Secrets are typed as `SecretStr` so they never appear in reprs or logs.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Language = Literal["darija", "ar", "fr"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    secret_key: SecretStr = SecretStr("change-me")
    encryption_key: SecretStr = SecretStr("")

    # Host names the app answers to (comma separated). "*" only for local dev.
    allowed_hosts: str = "*"
    # Max request body (bytes) for every endpoint.
    max_body_bytes: int = Field(default=1_000_000, ge=10_000, le=20_000_000)

    # Database
    database_url: str = "sqlite:///./cod_confirm.db"

    # WhatsApp Cloud API
    dry_run: bool = True
    meta_graph_api_version: str = "v21.0"
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    # Meta has no "Darija" language: Latin-script Darija templates are registered under
    # this language code (see docs/whatsapp-templates.md).
    whatsapp_darija_template_language: str = "fr"
    whatsapp_timeout_seconds: float = 10.0
    whatsapp_max_attempts: int = Field(default=3, ge=1, le=6)

    # Intent detection: optional LLM fallback when the rules don't understand a reply.
    intent_llm_enabled: bool = False
    anthropic_api_key: SecretStr = SecretStr("")
    intent_llm_model: str = "claude-opus-5-5"

    # Shop-owner alerts (modification requests, questions) through a Telegram bot.
    telegram_bot_token: SecretStr = SecretStr("")
    # Monitoring: errors pushed to the operator's Telegram chat, optional Sentry.
    operator_telegram_chat_id: str = ""
    sentry_dsn: SecretStr = SecretStr("")

    # Google Sheets sync: path to the service-account JSON key (optional).
    google_service_account_file: str = ""

    # Conversation safety: max "I didn't understand" replies per order.
    max_clarifications: int = Field(default=2, ge=0, le=5)

    # Scheduler (reminders, timeouts, daily summary). Run it in ONE process only.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = Field(default=60, ge=10, le=3600)
    # Never send business-initiated messages to customers in this local time range.
    quiet_hours_start: int = Field(default=22, ge=0, le=23)
    quiet_hours_end: int = Field(default=8, ge=0, le=23)
    # Hours after the last reminder before an unanswered order becomes NO_ANSWER.
    no_answer_after_hours: int = Field(default=24, ge=1, le=168)
    # Local hour at which the daily summary is sent to the owner.
    daily_summary_hour: int = Field(default=21, ge=0, le=23)
    # Give up sending a confirmation template after this many failures.
    max_send_failures: int = Field(default=3, ge=1, le=10)

    # Defaults
    default_timezone: str = "Africa/Tunis"
    default_language: Language = "darija"
    default_currency: str = Field(default="TND", min_length=3, max_length=3)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Render / Railway / Heroku give 'postgres://...': use the psycopg 3 driver."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @model_validator(mode="after")
    def _check_prod_secrets(self) -> Settings:
        """Refuse to start in production with placeholder / missing secrets."""
        if self.app_env == "prod":
            if self.secret_key.get_secret_value() in ("", "change-me"):
                raise ValueError("SECRET_KEY must be set to a strong random value in prod")
            if len(self.secret_key.get_secret_value()) < 32:
                raise ValueError("SECRET_KEY must be at least 32 characters in prod")
            if not self.dry_run and not self.meta_app_secret.get_secret_value():
                raise ValueError("META_APP_SECRET is required when DRY_RUN=false in prod")
            if self.allowed_hosts.strip() in ("", "*"):
                raise ValueError("ALLOWED_HOSTS must list your domain(s) in prod")
            keys = [k.strip() for k in self.encryption_key.get_secret_value().split(",")]
            try:
                if not any(keys) or any(
                    len(base64.urlsafe_b64decode(k.encode())) != 32 for k in keys
                ):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("ENCRYPTION_KEY must be one or more Fernet keys in prod") from exc
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance (call `get_settings.cache_clear()` in tests)."""
    return Settings()
