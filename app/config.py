"""Application settings, loaded from environment variables / `.env`.

Secrets are typed as `SecretStr` so they never appear in reprs or logs.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # Defaults
    default_timezone: str = "Africa/Tunis"
    default_language: Language = "darija"
    default_currency: str = Field(default="TND", min_length=3, max_length=3)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

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
            key = self.encryption_key.get_secret_value()
            try:
                if len(base64.urlsafe_b64decode(key.encode())) != 32:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("ENCRYPTION_KEY must be a Fernet key in prod") from exc
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance (call `get_settings.cache_clear()` in tests)."""
    return Settings()
