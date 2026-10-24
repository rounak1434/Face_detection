"""Tests with the real YuNet model and a public-domain face fixture
(tests/data/public_domain/README.md). Skipped only if the verified
model file is absent: run `python -m inference_service.download --models-dir models`.

FACE_FIXTURE=/path/photo.jpg adds a check on a photo of your own choosing.
"""

import os
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from inference_service.config import Settings
from inference_service.imaging import decode
from inference_service.main import create_app
from inference_service.registry import READY, YUNET, verify_file

from . import fixtures as fx

MODELS_DIR = Path(os.environ.get("INFERENCE_MODELS_DIR", Path(__file__).resolve().parents[1] / "models"))
PORTRAIT = Path(__file__).resolve().parent / "data" / "public_domain" / "nasa_s69-31741_480x600.jpg"

pytestmark = [
    pytest.mark.model,
    pytest.mark.skipif(verify_file(YUNET, MODELS_DIR)[0] != READY, reason="verified YuNet model not present"),
]


@pytest.fixture(scope="module")
def detector():
    from inference_service.adapters.yunet import YuNetDetector

    return YuNetDetector(MODELS_DIR / YUNET.file_name, max_side=1280)


@pytest.fixture(scope="module")
def portrait() -> np.ndarray:
    image, _ = decode(PORTRAIT.read_bytes(), 10 * 1024 * 1024, 40_000_000)
    return image


@pytest.fixture(scope="module")
def service():
    with TestClient(create_app(Settings(models_dir=MODELS_DIR))) as client:
        yield client


def _inside(points, box, slack=0.2):
    return all(box.x - box.w * slack <= x <= box.x + box.w * (1 + slack)
               and box.y - box.h * slack <= y <= box.y + box.h * (1 + slack) for x, y in points)


# --- one, several, none ---------------------------------------------------------

def test_one_face(detector, portrait):
    faces = detector.detect(portrait, 0.7, 50)
    assert len(faces) == 1
    face = faces[0]
    h, w = portrait.shape[:2]
    assert face.score >= 0.7
    assert 0 <= face.box.x and face.box.x + face.box.w <= w and 0 <= face.box.y and face.box.y + face.box.h <= h
    # The face is in the upper middle of this portrait.
    assert w * 0.3 < face.box.x + face.box.w / 2 < w * 0.8 and face.box.y + face.box.h / 2 < h * 0.5
    assert len(face.keypoints5) == 5 and _inside(face.keypoints5, face.box)


def test_multiple_faces_positions_match(detector, portrait):
    single = detector.detect(portrait, 0.7, 50)[0]
    width = portrait.shape[1]
    faces = detector.detect(np.hstack([portrait, portrait]), 0.7, 50)
    assert len(faces) == 2
    xs = sorted(f.box.x for f in faces)
    assert abs(xs[0] - single.box.x) <= 3 and abs(xs[1] - (single.box.x + width)) <= 3
    three = np.hstack([portrait, portrait, cv2.flip(portrait, 1)])
    assert len(detector.detect(three, 0.7, 50)) == 3


def test_max_faces_limits_results(detector, portrait):
    three = np.hstack([portrait, portrait, portrait])
    faces = detector.detect(three, 0.7, 2)
    assert len(faces) == 2
    assert faces[0].score >= faces[1].score


@pytest.mark.parametrize("image", [fx.solid(), fx.noise(), fx.shapes(), fx.solid(1920, 1080, 0)],
                         ids=["grey", "noise", "shapes", "black-1080p"])
def test_no_faces_in_synthetic_images(detector, image):
    assert detector.detect(image, 0.7, 50) == []


def test_handles_tiny_and_extreme_aspect_images(detector):
    # Regression: unpadded short inputs gave intermittent garbage faces
    # (about 5% of calls on 20x2000), so repeat and interleave other sizes.
    other = fx.noise(640, 360, seed=1)
    for _ in range(40):
        for shape in [(1, 1, 3), (16, 16, 3), (20, 2000, 3), (2000, 20, 3), (31, 31, 3)]:
            detector.detect(other, 0.7, 50)
            assert detector.detect(np.full(shape, 100, dtype=np.uint8), 0.7, 50) == []


# --- boundaries, scale, small faces ----------------------------------------------------

def test_large_image_is_scaled_and_mapped_back(detector, portrait):
    single = detector.detect(portrait, 0.7, 50)[0]
    big = cv2.resize(portrait, None, fx=3, fy=3, interpolation=cv2.INTER_LINEAR)  # 1440x1800 > max_side
    faces = detector.detect(big, 0.7, 50)
    assert len(faces) == 1
    assert abs(faces[0].box.x - single.box.x * 3) <= single.box.w * 3 * 0.15
    assert abs(faces[0].box.y - single.box.y * 3) <= single.box.h * 3 * 0.15


def test_small_face_is_detected_and_flagged(service, portrait):
    single_h = 102  # measured face height in the fixture
    scale = 24 / single_h
    small = cv2.resize(portrait, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    canvas = np.full((480, 640, 3), 90, np.uint8)
    canvas[:small.shape[0], :small.shape[1]] = small
    body = service.post("/v1/detect", content=fx.encode(canvas[:, :, ::-1], "PNG")).json()
    assert body["face_count"] == 1
    assert body["faces"][0]["quality"]["small_face"] is True
    assert body["faces"][0]["quality"]["face_px"] < 40


# --- the internal HTTP API with the real model ---------------------------------------

def test_service_reports_ready(service):
    health = service.get("/health").json()
    assert health["ready"] is True and health["models"] == {"yunet": "ready"}
    model = service.get("/v1/models").json()[0]
    assert model["status"] == "ready" and model["sha256"] == YUNET.sha256 and model["license"] == "MIT"


def test_detect_endpoint_with_real_model(service):
    resp = service.post("/v1/detect", content=PORTRAIT.read_bytes())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "faces_detected" and body["face_count"] == 1
    face = body["faces"][0]
    crop = face["crop"]
    # Crop = face box plus 20% margin on each side, inside the image.
    assert crop["box"]["x"] < face["box"]["x"] and crop["box"]["x"] + crop["box"]["w"] > face["box"]["x"] + face["box"]["w"]
    assert (crop["width"], crop["height"]) == (crop["box"]["w"], crop["box"]["h"])
    assert face["quality"]["touches_edge"] is False and face["quality"]["sharpness"] > 0
    assert body["model"] == {"name": "yunet", "version": "2023mar"}


def test_face_near_corner_crop_is_clipped(service, portrait):
    face = service.post("/v1/detect", content=PORTRAIT.read_bytes()).json()["faces"][0]["box"]
    near = portrait[face["y"] - 4:, face["x"] - 4:]  # face now 4 px from the top-left corner
    body = service.post("/v1/detect", content=fx.encode(near[:, :, ::-1], "PNG")).json()
    assert body["face_count"] == 1
    crop = body["faces"][0]["crop"]["box"]
    assert crop["x"] == 0 and crop["y"] == 0  # the 20% margin was clipped at the border
    assert crop["w"] > 0 and crop["h"] > 0


def test_face_cut_by_image_edge_is_flagged(service, portrait):
    face = service.post("/v1/detect", content=PORTRAIT.read_bytes()).json()["faces"][0]["box"]
    cut = portrait[:, face["x"] + face["w"] // 3:]  # left third of the face removed
    body = service.post("/v1/detect", content=fx.encode(cut[:, :, ::-1], "PNG")).json()
    assert body["face_count"] == 1
    result = body["faces"][0]
    width, height = body["image"]["width"], body["image"]["height"]
    assert result["box"]["x"] == 0
    assert result["quality"]["touches_edge"] is True
    # Box and crop are clipped to the image...
    for b in (result["box"], result["crop"]["box"]):
        assert 0 <= b["x"] and b["x"] + b["w"] <= width and 0 <= b["y"] and b["y"] + b["h"] <= height
    # ...but keypoints are the model's raw output: for this face YuNet puts
    # the cut-off eye outside the image (measured x of about -3.7). The API
    # keeps them unmodified; the UI clamps them for drawing only.
    xs = [p[0] for p in result["keypoints5"]]
    assert min(xs) < 0, xs
    assert all(-result["box"]["w"] <= x <= width for x in xs)


def test_no_face_through_service(service):
    body = service.post("/v1/detect", content=fx.encode(fx.shapes(), "JPEG")).json()
    assert body["status"] == "no_faces_detected" and body["faces"] == []


# --- optional: your own photo ----------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("FACE_FIXTURE"), reason="FACE_FIXTURE not set")
def test_detects_a_face_in_your_photo(detector):
    image, _ = decode(Path(os.environ["FACE_FIXTURE"]).read_bytes(), 20 * 1024 * 1024, 40_000_000)
    faces = detector.detect(image, 0.7, 50)
    assert faces and len(faces[0].keypoints5) == 5 and _inside(faces[0].keypoints5, faces[0].box)
