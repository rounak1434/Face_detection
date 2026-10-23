"""HTTP contract of the inference service, with a fake detector (no model)."""

import base64

import pytest
from fastapi.testclient import TestClient

from inference_service.adapters.base import Box, DetectedFace
from inference_service.config import Settings
from inference_service.main import create_app

from . import fixtures as fx


class FakeDetector:
    name = "fake"
    version = "test"

    def __init__(self, faces=None):
        self.faces = faces if faces is not None else []
        self.calls = []

    def detect(self, image, score_threshold, max_faces):
        self.calls.append((image.shape, score_threshold, max_faces))
        return [f for f in self.faces if f.score >= score_threshold][:max_faces]


def _face(x, y, w, h, score=0.9):
    return DetectedFace(Box(x, y, w, h), score, [(x + 5.0, y + 5.0)] * 5)


@pytest.fixture
def settings(tmp_path):
    return Settings(models_dir=tmp_path, max_image_bytes=2 * 1024 * 1024, max_image_pixels=10_000_000)


def client_with(settings, detector):
    return TestClient(create_app(settings, detector=detector))


def test_detect_returns_boxes_keypoints_and_crops(settings):
    det = FakeDetector([_face(100, 50, 80, 100, 0.95), _face(10, 10, 40, 40, 0.8)])
    with client_with(settings, det) as c:
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(320, 240), "JPEG"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["image"] == {"width": 320, "height": 240, "format": "image/jpeg"}
    assert body["face_count"] == 2
    first = body["faces"][0]
    assert first["box"] == {"x": 100, "y": 50, "w": 80, "h": 100}
    assert len(first["keypoints5"]) == 5
    # 20% margin, clipped to the image.
    assert first["crop"]["box"] == {"x": 84, "y": 30, "w": 112, "h": 140}
    assert base64.b64decode(first["crop"]["jpeg_base64"])[:3] == b"\xff\xd8\xff"
    assert body["faces"][1]["crop"]["box"]["x"] == 2
    assert body["model"] == {"name": "fake", "version": "test"}


def test_no_face_is_a_normal_result(settings):
    with client_with(settings, FakeDetector([])) as c:
        body = c.post("/v1/detect", content=fx.encode(fx.solid(), "PNG")).json()
    assert body["faces"] == [] and body["face_count"] == 0


def test_threshold_and_limit_parameters(settings):
    det = FakeDetector([_face(0, 0, 10, 10, 0.95), _face(20, 20, 10, 10, 0.75), _face(40, 40, 10, 10, 0.72)])
    with client_with(settings, det) as c:
        body = c.post("/v1/detect?min_score=0.9", content=fx.encode(fx.solid(), "JPEG")).json()
        assert body["face_count"] == 1
        body = c.post("/v1/detect?max_faces=2&min_score=0.7&crops=false", content=fx.encode(fx.solid(), "JPEG")).json()
        assert body["face_count"] == 2
        assert "crop" not in body["faces"][0]


@pytest.mark.parametrize(
    "data, status, code",
    [
        (fx.GIF, 415, "unsupported_format"),
        (b"", 400, "invalid_image"),
        (b"\xff\xd8\xff\xe0garbage", 400, "invalid_image"),
        (fx.png_header_only(20_000, 20_000), 413, "too_large"),
    ],
)
def test_input_errors(settings, data, status, code):
    with client_with(settings, FakeDetector()) as c:
        resp = c.post("/v1/detect", content=data)
    assert resp.status_code == status
    assert resp.json()["error"] == code


def test_body_over_limit_rejected(settings):
    with client_with(settings, FakeDetector()) as c:
        resp = c.post("/v1/detect", content=b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024))
    assert resp.status_code == 413


def test_model_missing_reports_unavailable(settings):
    # No detector injected and no model file in models_dir.
    with TestClient(create_app(settings)) as c:
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
        models = c.get("/v1/models").json()
        health = c.get("/v1/health").json()
    assert resp.status_code == 503
    assert resp.json()["error"] == "model_unavailable"
    assert resp.json()["model_status"] == "missing"
    assert models[0]["name"] == "yunet" and models[0]["status"] == "missing"
    assert models[0]["license"] == "MIT"
    assert health["models"] == {"yunet": "missing"}


def test_corrupted_model_file_is_not_loaded(settings):
    (settings.models_dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"not a model")
    with TestClient(create_app(settings)) as c:
        status = c.get("/v1/models").json()[0]["status"]
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
    assert status == "checksum_mismatch"
    assert resp.status_code == 503


def test_detector_crash_is_contained(settings):
    class Broken(FakeDetector):
        def detect(self, *args):
            raise RuntimeError("boom")

    with client_with(settings, Broken()) as c:
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
    assert resp.status_code == 500
    assert resp.json()["error"] == "inference_failed"
    assert "boom" not in resp.text
