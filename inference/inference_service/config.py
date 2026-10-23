"""Settings, from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INFERENCE_", extra="ignore")

    models_dir: Path = Path("./models")
    # Upload limits. The pixel limit protects against decompression bombs:
    # a small file that decodes to a huge bitmap.
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_image_pixels: int = Field(default=40_000_000, ge=1)
    # Detection defaults (callers may tighten them per request).
    detection_score_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_faces: int = Field(default=50, ge=1)
    # YuNet works best on moderate resolutions; larger images are scaled down
    # for detection and the results scaled back to original coordinates.
    detection_max_side: int = Field(default=1280, ge=160)
    crop_margin: float = Field(default=0.2, ge=0.0, le=1.0)
    crop_jpeg_quality: int = Field(default=90, ge=50, le=100)
    # Faces whose shorter box side is below this are flagged "small" (still returned).
    small_face_px: int = Field(default=40, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
