"""/api/analysis/faces and /api/models with the inference service mocked.

These test the API's contract and error mapping. Real detection is tested
in inference/tests (real YuNet model) and tests/integration (whole stack).
"""

import base64

import httpx
import pytest

from .conftest import INFERENCE

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 10
CROP = base64.b64encode(b"\xff\xd8\xffcrop").decode()

DETECT_OK = {
    "image": {"width": 640, "height": 480, "format": "image/jpeg"},
    "faces": [{
        "index": 0, "box": {"x": 100, "y": 80, "w": 120, "h": 150}, "score": 0.94,
        "keypoints5": [[130, 140], [190, 138], [160, 170], [135, 200], [185, 198]],
        "crop": {"box": {"x": 76, "y": 50, "w": 168, "h": 210}, "jpeg_base64": CROP},
    }],
    "face_count": 1,
    "parameters": {"min_score": 0.7, "max_faces": 50, "crop_margin": 0.2},
    "model": {"name": "yunet", "version": "2023mar"},
    "processing_ms": 12.5,
}


@pytest.fixture
def inference(frigate):
    """Routes on the shared respx router for the inference host."""
    return frigate


def _post(client, data=JPEG, content_type="image/jpeg", params=""):
    return client.post(f"/api/analysis/faces{params}", content=data, headers={"Content-Type": content_type})


def test_requires_login(client):
    assert _post(client).status_code == 401
    assert client.get("/api/models").status_code == 401


def test_faces_found(signed_in, inference):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, params="?min_score=0.8&max_faces=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "inference"
    assert body["stored"] is False
    assert body["face_count"] == 1 and body["message"] is None
    face = body["faces"][0]
    assert face["box"] == {"x": 100, "y": 80, "w": 120, "h": 150}
    assert face["crop_box"] == {"x": 76, "y": 50, "w": 168, "h": 210}
    assert face["crop_data_url"] == f"data:image/jpeg;base64,{CROP}"
    assert body["model"] == {"name": "yunet", "version": "2023mar"}
    sent = route.calls.last.request
    assert sent.content == JPEG
    assert sent.url.params["min_score"] == "0.8" and sent.url.params["max_faces"] == "5"


def test_no_face_is_success_with_message(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").respond(200, json={**DETECT_OK, "faces": [], "face_count": 0})
    body = _post(signed_in, PNG, "image/png").json()
    assert body["faces"] == [] and body["message"] == "No face detected"


@pytest.mark.parametrize(
    "data, ctype, status, code",
    [
        (JPEG, "image/gif", 415, "unsupported_format"),
        (JPEG, "application/octet-stream", 415, "unsupported_format"),
        (b"GIF89a....", "image/jpeg", 415, "unsupported_format"),  # declared type not trusted
        (b"", "image/jpeg", 400, "invalid_image"),
    ],
)
def test_upload_validation(signed_in, inference, data, ctype, status, code):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, data, ctype)
    assert resp.status_code == status
    assert resp.json()["error"] == code
    assert not route.called  # rejected before reaching the model


def test_size_limit(signed_in, inference, settings):
    settings.analysis_max_image_bytes = 1000
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, b"\xff\xd8\xff" + b"0" * 2000)
    assert resp.status_code == 413 and resp.json()["error"] == "too_large"
    assert not route.called


@pytest.mark.parametrize(
    "status, payload, expect_status, expect_code",
    [
        (400, {"error": "invalid_image", "detail": "The file could not be decoded as an image"}, 400, "invalid_image"),
        (413, {"error": "too_large", "detail": "Image has 9000x9000 pixels"}, 413, "too_large"),
        (503, {"error": "model_unavailable", "detail": "missing", "model": "yunet", "model_status": "missing"},
         503, "model_unavailable"),
        (500, {"error": "inference_failed", "detail": "The face detector failed on this image"}, 500, "inference_failed"),
        (500, {"error": "something_internal", "detail": "Traceback ..."}, 502, "inference_failed"),
    ],
)
def test_inference_errors_are_mapped(signed_in, inference, status, payload, expect_status, expect_code):
    inference.post(f"{INFERENCE}/v1/detect").respond(status, json=payload)
    resp = _post(signed_in)
    assert resp.status_code == expect_status
    body = resp.json()
    assert body["error"] == expect_code
    assert "Traceback" not in resp.text
    if expect_code == "model_unavailable":
        assert body["model_status"] == "missing"


def test_inference_unreachable(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").mock(side_effect=httpx.ConnectError("refused"))
    resp = _post(signed_in)
    assert resp.status_code == 503
    assert resp.json()["error"] == "inference_unavailable"


def test_inference_timeout(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").mock(side_effect=httpx.ReadTimeout("slow"))
    resp = _post(signed_in)
    assert resp.status_code == 503
    assert "did not answer in time" in resp.json()["detail"]


def test_non_json_inference_response(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").respond(200, text="<html>proxy error</html>")
    assert _post(signed_in).status_code == 502


def test_models_listing(signed_in, inference):
    inference.get(f"{INFERENCE}/v1/models").respond(200, json=[{
        "name": "yunet", "task": "face_detection", "version": "2023mar", "status": "ready", "detail": None,
        "license": "MIT", "license_url": "https://example", "source": "https://example", "notes": "n",
        "runtime": "opencv 4.14.0", "sha256": "abc",
    }])
    body = signed_in.get("/api/models").json()
    assert body["inference"] == {"reachable": True}
    assert body["models"][0]["status"] == "ready"
    assert "sha256" not in body["models"][0]


def test_models_listing_when_unreachable(signed_in, inference):
    inference.get(f"{INFERENCE}/v1/models").mock(side_effect=httpx.ConnectError("refused"))
    body = signed_in.get("/api/models").json()
    assert body["inference"]["reachable"] is False and body["models"] == []


def test_health_reports_inference(client, inference):
    assert client.get("/api/health").json()["inference"] == {"reachable": True, "models_ready": True}
    inference.get(f"{INFERENCE}/v1/health").respond(200, json={"status": "ok", "models": {"yunet": "missing"}})
    assert client.get("/api/health").json()["inference"]["models_ready"] is False
    inference.get(f"{INFERENCE}/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    assert client.get("/api/health").json()["inference"]["reachable"] is False
