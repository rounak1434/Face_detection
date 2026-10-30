"""Runtime settings, read from environment variables (or a local .env file).

Every value has a safe default for local development. Secrets have no
default and are never logged.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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
    # Ingestion of finished Frigate person events into face events.
    face_events_enabled: bool = True
    face_poll_seconds: float = Field(default=10.0, gt=0)
    # Frigate filters events by start time, so each poll looks back this far from the newest
    # stored event: an event longer than this that ends later is missed.
    face_event_lookback_seconds: int = Field(default=3600, ge=60)
    # On the very first run (no face events stored yet), start this far in the past.
    face_backfill_seconds: int = Field(default=3600, ge=0)
    # An event that keeps failing for a temporary reason is recorded as "gave_up" after this many tries.
    face_max_attempts: int = Field(default=3, ge=1)
    # Face events (rows, frames, crops) older than this many days are deleted hourly; 0 keeps them.
    face_event_retention_days: int = Field(default=30, ge=0)

    # Gallery enrollment (ADR-0004, Phase 5): an uploaded reference image must contain exactly one
    # face. Every face the detector finds at or above gallery_other_face_score counts; the one face
    # must then reach gallery_min_score, be at least gallery_min_face_px wide and high, and not touch
    # the image edge. Starting points, not measured thresholds.
    gallery_min_score: float = Field(default=0.9, ge=0.0, le=1.0)
    gallery_other_face_score: float = Field(default=0.5, ge=0.0, le=1.0)
    gallery_min_face_px: int = Field(default=80, ge=1)
    gallery_max_faces_per_person: int = Field(default=10, ge=1, le=100)

    # Recognition (ADR-0004, Phase 6), only while switched on in the app. Cosine similarity of
    # SFace embeddings, best reference face per person: >= accept is AUTHORIZED, <= reject is
    # UNRECOGNIZED, in between INCONCLUSIVE. Starting points from OpenCV's published 0.363 and four
    # public-domain portraits (same person 0.617, different people 0.118-0.194); must be measured
    # on the real camera before relying on them. Faces smaller than recognition_min_face_px are
    # INCONCLUSIVE without matching.
    recognition_accept_threshold: float = Field(default=0.45, ge=-1.0, le=1.0)
    recognition_reject_threshold: float = Field(default=0.30, ge=-1.0, le=1.0)
    recognition_min_face_px: int = Field(default=40, ge=1)

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> "Settings":
        if self.recognition_reject_threshold >= self.recognition_accept_threshold:
            raise ValueError("RECOGNITION_REJECT_THRESHOLD must be below RECOGNITION_ACCEPT_THRESHOLD")
        return self

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
