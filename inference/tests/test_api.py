"""HTTP contract of the inference service, with a fake detector (no model)."""

import base64

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

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
    assert body["status"] == "faces_detected"
    first = body["faces"][0]
    assert first["box"] == {"x": 100, "y": 50, "w": 80, "h": 100}
    assert len(first["keypoints5"]) == 5
    # 20% margin, clipped to the image.
    assert first["crop"]["box"] == {"x": 84, "y": 30, "w": 112, "h": 140}
    assert (first["crop"]["width"], first["crop"]["height"]) == (112, 140)
    assert base64.b64decode(first["crop"]["jpeg_base64"])[:3] == b"\xff\xd8\xff"
    assert first["quality"]["face_px"] == 80 and first["quality"]["small_face"] is False
    assert body["faces"][1]["crop"]["box"]["x"] == 2
    assert body["model"] == {"name": "fake", "version": "test"}


TOKEN = "test-service-token-0123456789"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": f"Basic {TOKEN}"}])
def test_token_required_when_configured(settings, headers):
    settings.api_token = SecretStr(TOKEN)
    det = FakeDetector([_face(10, 10, 40, 40)])
    with client_with(settings, det) as c:
        detect = c.post("/v1/detect", content=fx.encode(fx.solid(), "PNG"), headers=headers)
        models = c.get("/v1/models", headers=headers)
    assert detect.status_code == models.status_code == 401
    assert detect.json()["error"] == "unauthorized"
    assert TOKEN not in detect.text and TOKEN not in models.text
    assert det.calls == []  # refused before any work


def test_correct_token_is_accepted_and_health_stays_open(settings):
    settings.api_token = SecretStr(TOKEN)
    with client_with(settings, FakeDetector([])) as c:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        assert c.post("/v1/detect", content=fx.encode(fx.solid(), "PNG"), headers=auth).status_code == 200
        assert c.get("/v1/models", headers=auth).status_code == 200
        # The container health check has no token; it reveals readiness only.
        assert c.get("/health").json()["ready"] is True
        assert c.get("/v1/health").status_code == 200


def test_no_token_configured_keeps_the_service_open(settings):
    assert settings.api_token is None
    with client_with(settings, FakeDetector([])) as c:
        assert c.post("/v1/detect", content=fx.encode(fx.solid(), "PNG")).status_code == 200


def test_no_face_is_a_normal_result(settings):
    with client_with(settings, FakeDetector([])) as c:
        body = c.post("/v1/detect", content=fx.encode(fx.solid(), "PNG")).json()
    assert body["faces"] == [] and body["face_count"] == 0
    assert body["status"] == "no_faces_detected"


def test_threshold_and_limit_parameters(settings):
    det = FakeDetector([_face(0, 0, 10, 10, 0.95), _face(20, 20, 10, 10, 0.75), _face(40, 40, 10, 10, 0.72)])
    with client_with(settings, det) as c:
        body = c.post("/v1/detect?min_score=0.9", content=fx.encode(fx.solid(), "JPEG")).json()
        assert body["face_count"] == 1
        body = c.post("/v1/detect?max_faces=2&min_score=0.7&crops=false", content=fx.encode(fx.solid(), "JPEG")).json()
        assert body["face_count"] == 2
        assert body["faces"][0]["crop"] is None


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
        health = c.get("/health").json()
    assert resp.status_code == 503
    assert resp.json()["error"] == "model_unavailable"
    assert resp.json()["model_status"] == "missing"
    assert models[0]["name"] == "yunet" and models[0]["status"] == "missing"
    assert models[0]["license"] == "MIT"
    assert health["models"] == {"yunet": "missing"} and health["ready"] is False
    assert_no_path(settings.models_dir, resp, models)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def assert_no_path(models_dir, *payloads):
    """No part of the models directory path may reach a client.

    Checks the parsed string values, not the raw JSON text: JSON escapes
    backslashes, which hid a Windows path from the old raw-text check. The
    directory's unique name (no separators) is checked as well.
    """
    for payload in payloads:
        data = payload.json() if hasattr(payload, "json") else payload
        for s in _strings(data):
            assert str(models_dir) not in s and models_dir.name not in s, s


def test_corrupted_model_file_is_not_loaded(settings):
    (settings.models_dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"not a model")
    with TestClient(create_app(settings)) as c:
        models = c.get("/v1/models").json()
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
    assert models[0]["status"] == "checksum_mismatch"
    assert resp.status_code == 503
    assert_no_path(settings.models_dir, resp, models)


def test_model_that_fails_to_load_is_reported_not_ready(settings, monkeypatch):
    import inference_service.main as main_mod

    monkeypatch.setattr(main_mod, "verify_file", lambda spec, d: ("ready", None))

    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("bad onnx graph at /secret/path")

    import inference_service.adapters.yunet as yunet_mod

    monkeypatch.setattr(yunet_mod, "YuNetDetector", Exploding)
    with TestClient(create_app(settings)) as c:
        health = c.get("/health").json()
        model = c.get("/v1/models").json()[0]
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
    assert health["ready"] is False and model["status"] == "load_error"
    assert resp.status_code == 503 and resp.json()["model_status"] == "load_error"
    assert "/secret/path" not in resp.text and "/secret/path" not in str(model)
    assert_no_path(settings.models_dir, resp, model)


def test_health_ready_with_loaded_detector(settings):
    with client_with(settings, FakeDetector()) as c:
        assert c.get("/health").json()["ready"] is True
        assert c.get("/v1/health").json()["ready"] is True


def test_detector_crash_is_contained(settings):
    class Broken(FakeDetector):
        def detect(self, *args):
            raise RuntimeError("boom")

    with client_with(settings, Broken()) as c:
        resp = c.post("/v1/detect", content=fx.encode(fx.solid(), "JPEG"))
    assert resp.status_code == 500
    assert resp.json()["error"] == "inference_failed"
    assert "boom" not in resp.text
