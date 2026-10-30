"""Real SFace embeddings on the public-domain NASA portraits (tests/data/public_domain/README.md).

Skipped unless both verified model files are present:
`python -m inference_service.download --models-dir models`.

What this shows: the same photo stays close to itself under ordinary changes, two photos of the
same person taken five years apart are closer than photos of different people, and three
different people are below OpenCV's published SFace cosine threshold (0.363). Four photos of
three people is a plumbing and sanity check, not an accuracy measurement: thresholds for the real
door camera must be measured on site (ADR-0004, Phase 10).
"""

import base64
import json
import os
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from inference_service.config import Settings
from inference_service.main import create_app
from inference_service.registry import READY, SFACE, YUNET, verify_file

from . import fixtures as fx

MODELS_DIR = Path(os.environ.get("INFERENCE_MODELS_DIR", Path(__file__).resolve().parents[1] / "models"))
DATA = Path(__file__).resolve().parent / "data" / "public_domain"
PHOTOS = {
    "armstrong_1969": DATA / "nasa_s69-31741_480x600.jpg",
    "armstrong_1964": DATA / "nasa_armstrong_1964_480x600.jpg",
    "aldrin_1969": DATA / "nasa_aldrin_1969_480x600.jpg",
    "collins_1969": DATA / "nasa_collins_s69-31742_480x600.jpg",
}
OPENCV_COSINE_THRESHOLD = 0.363  # OpenCV's FaceRecognizerSF sample value; a reference, not our setting

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(any(verify_file(s, MODELS_DIR)[0] != READY for s in (YUNET, SFACE)),
                       reason="verified YuNet and SFace models not present"),
]


@pytest.fixture(scope="module")
def service():
    with TestClient(create_app(Settings(models_dir=MODELS_DIR, _env_file=None))) as client:
        yield client


def _embed(service, data: bytes) -> np.ndarray:
    resp = service.post("/v1/embed?min_score=0.7", content=data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["face_count"] == 1, body["face_count"]
    assert body["embedder"] == {"name": "sface", "version": "2021dec", "dimensions": 128}
    return np.frombuffer(base64.b64decode(body["faces"][0]["embedding"]), dtype="<f4")


@pytest.fixture(scope="module")
def vectors(service) -> dict[str, np.ndarray]:
    return {name: _embed(service, path.read_bytes()) for name, path in PHOTOS.items()}


def test_service_reports_both_models_ready(service):
    health = service.get("/health").json()
    assert health["ready"] is True and health["embed_ready"] is True
    assert health["models"] == {"yunet": "ready", "sface": "ready"}


def test_embeddings_are_unit_vectors(vectors):
    for name, v in vectors.items():
        assert v.shape == (128,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-4, name


def _variants(path: Path) -> dict[str, bytes]:
    image = cv2.imread(str(path))
    h, w = image.shape[:2]
    return {
        "jpeg_q40": cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 40])[1].tobytes(),
        "downscaled_70pct": cv2.imencode(".jpg", cv2.resize(image, (int(w * 0.7), int(h * 0.7))))[1].tobytes(),
        "brighter": cv2.imencode(".jpg", cv2.convertScaleAbs(image, alpha=1.0, beta=35))[1].tobytes(),
        "mirrored": cv2.imencode(".jpg", cv2.flip(image, 1))[1].tobytes(),
        "png": fx.encode(image[:, :, ::-1], "PNG"),
    }


def test_same_photo_stays_close_under_ordinary_changes(service, vectors):
    sims = {k: float(vectors["armstrong_1969"] @ _embed(service, data))
            for k, data in _variants(PHOTOS["armstrong_1969"]).items()}
    print(json.dumps({"same_photo_variants": {k: round(v, 3) for k, v in sims.items()}}))
    assert min(sims.values()) > 0.7, sims


def test_people_are_separated(vectors):
    sims = {f"{a}~{b}": round(float(vectors[a] @ vectors[b]), 3) for a, b in combinations(sorted(vectors), 2)}
    print(json.dumps({"pairwise_cosine": sims}))
    same = sims["armstrong_1964~armstrong_1969"]
    different = [v for k, v in sims.items() if k != "armstrong_1964~armstrong_1969"]
    assert same > OPENCV_COSINE_THRESHOLD, sims
    assert max(different) < OPENCV_COSINE_THRESHOLD, sims
    assert same > max(different), sims
