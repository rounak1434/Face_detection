"""Model registry: what each model is, where it comes from, and whether it is usable.

Every model file is pinned by SHA-256 and size. A file that is missing or
does not match is reported as such; it is never loaded.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    name: str
    task: str
    version: str
    file_name: str
    sha256: str
    size_bytes: int
    url: str
    source: str
    license: str
    license_url: str
    notes: str = ""


YUNET = ModelSpec(
    name="yunet",
    task="face_detection",
    version="2023mar",
    file_name="face_detection_yunet_2023mar.onnx",
    # From the Git LFS pointer in opencv/opencv_zoo (checked 2026-10-23).
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size_bytes=232589,
    url="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
    license="MIT",
    license_url="https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
    notes="Face boxes, a confidence score and 5 keypoints (eyes, nose tip, mouth corners). Not liveness detection.",
)

SFACE = ModelSpec(
    name="sface",
    task="face_embedding",
    version="2021dec",
    file_name="face_recognition_sface_2021dec.onnx",
    # From the Git LFS pointer in opencv/opencv_zoo (checked in ADR-0004, Phase 1).
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    size_bytes=38696353,
    url="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    source="https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface",
    license="Apache-2.0",
    license_url="https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface",
    notes="128-value face embedding of a face aligned on YuNet's 5 keypoints; compared by cosine similarity. "
          "Says how alike two faces look, not who anyone is; thresholds must be checked on site. Not liveness detection.",
)

MODELS: dict[str, ModelSpec] = {YUNET.name: YUNET, SFACE.name: SFACE}

# Status values reported by /v1/models.
READY = "ready"
MISSING = "missing"
CHECKSUM_MISMATCH = "checksum_mismatch"
LOAD_ERROR = "load_error"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(spec: ModelSpec, models_dir: Path) -> tuple[str, str | None]:
    """Return (status, detail) for the model file on disk.

    The detail is shown to API clients, so it never contains filesystem paths.
    """
    path = models_dir / spec.file_name
    if not path.is_file():
        return MISSING, f"{spec.file_name} is not installed in the models directory"
    if path.stat().st_size != spec.size_bytes:
        return CHECKSUM_MISMATCH, f"size {path.stat().st_size} != expected {spec.size_bytes}"
    if sha256_of(path) != spec.sha256:
        return CHECKSUM_MISMATCH, "SHA-256 does not match the pinned value"
    return READY, None
