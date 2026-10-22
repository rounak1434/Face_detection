"""Runtime settings, read from environment variables (or a local .env file).

Every value has a safe default for local development. Secrets have no
default and are never logged.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Display name only; change it to rebrand without touching code.
    app_display_name: str = "AI Surveillance"

    database_url: str = "sqlite:///./data/app.db"
    media_root: Path = Path("./data/media")

    # Frigate is reached only from the backend, never from the browser.
    # Inside docker compose this is the internal network address.
    frigate_url: str = "http://frigate:5000"
    frigate_username: str | None = None
    frigate_password: SecretStr | None = None
    frigate_verify_tls: bool = True
    frigate_timeout_seconds: float = 10.0

    session_ttl_hours: int = 12
    # Set to false only for plain-HTTP local development.
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_lockout_seconds: int = 300

    clip_default_seconds: int = Field(default=10, ge=1)
    clip_max_seconds: int = Field(default=60, ge=1)
    # Frigate writes recordings in segments; wait for the last one to be
    # flushed to disk before asking for an export.
    clip_settle_seconds: float = 12.0
    clip_export_timeout_seconds: float = 180.0
    clip_poll_interval_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
