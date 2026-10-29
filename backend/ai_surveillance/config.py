"""Runtime settings, read from environment variables (or a local .env file).

Every value has a safe default for local development. Secrets have no
default and are never logged.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # "Apply to Frigate" (ADR-0003). Unset paths disable it (503). The template
    # is deploy/frigate/config.yml; the secrets directory is the volume Frigate
    # mounts read-only as /run/secrets.
    frigate_config_template: Path | None = None
    frigate_secrets_dir: Path | None = None
    # Frigate's config before the last apply, kept for manual recovery.
    frigate_previous_config_file: Path = Path("./data/frigate-config.previous.yml")
    # Each restart: Frigate must stop within the first value and answer again within the second.
    frigate_restart_down_seconds: float = Field(default=30.0, gt=0)
    frigate_restart_timeout_seconds: float = Field(default=180.0, gt=0)
    frigate_restart_poll_seconds: float = Field(default=1.0, ge=0)

    # Face detection on Frigate person events (ADR-0004). "recording" decodes the event's best
    # frame from the recording (usually the main stream: larger faces) and falls back to the
    # event snapshot (detect stream) when no recording covers that moment; "snapshot" always
    # uses the event snapshot.
    face_frame_source: Literal["recording", "snapshot"] = "recording"
    face_min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    face_max_faces: int = Field(default=10, ge=1, le=100)

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
    # Each clip is exported by Frigate and then copied here. By default the
    # Frigate export is deleted afterwards, so the clip does not take disk
    # space twice; true keeps it on Frigate's Exports page as well.
    clip_keep_frigate_export: bool = False

    # Captures share the data volume with the database. New captures are
    # refused while less than this much space is free there (0 disables).
    capture_min_free_mb: int = Field(default=1024, ge=0)
    # Delete snapshots and clips older than this many days, checked at
    # startup and hourly. 0 keeps them until an operator deletes them.
    capture_retention_days: int = Field(default=0, ge=0)

    # Camera/NVR credentials are encrypted with this key (32 bytes, base64).
    # If unset, a key is generated once and kept in credentials_key_file.
    credentials_key: SecretStr | None = None
    credentials_key_file: Path = Path("./data/credentials.key")

    # Connection tests may only reach devices in these networks (private
    # LAN ranges by default). Loopback, link-local and public addresses are
    # always refused.
    device_allowed_networks: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
    device_test_timeout_seconds: float = 10.0
    device_test_cooldown_seconds: float = 5.0

    # The synthetic RTSP source is defined by the server, never by the browser.
    synthetic_rtsp_url: str = "rtsp://test-stream:8554/test"

    # Internal face-analysis service (inference container); never exposed.
    inference_url: str = "http://inference:8100"
    inference_timeout_seconds: float = 30.0
    # Shared secret sent to the inference service (same INFERENCE_API_TOKEN
    # on both containers); unset means the service is called without one.
    inference_api_token: SecretStr | None = None
    analysis_max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    # Simultaneous analyses, counted from the start of the upload (memory and
    # CPU protection); more requests wait up to analysis_queue_timeout_seconds
    # for a slot, then get 503 analysis_busy.
    analysis_concurrency: int = Field(default=2, ge=1)
    analysis_queue_timeout_seconds: float = Field(default=15.0, gt=0)
    # A client must deliver the whole upload within this time (408 otherwise),
    # so a slow sender cannot hold a slot indefinitely.
    analysis_upload_timeout_seconds: float = Field(default=30.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
