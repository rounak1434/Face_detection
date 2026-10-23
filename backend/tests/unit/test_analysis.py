"""/api/analysis/faces and /api/models with the inference service mocked.

These test the API's contract, upload handling and error mapping. Real
detection is tested in inference/tests (real YuNet model) and in
tests/integration (whole stack).
"""

import base64

import httpx
import pytest

from .conftest import INFERENCE

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 10
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"x" * 20
CROP = base64.b64encode(b"\xff\xd8\xffcrop").decode()

DETECT_OK = {
    "status": "faces_detected",
    "image": {"width": 640, "height": 480, "format": "image/jpeg"},
    "faces": [{
        "index": 0, "box": {"x": 100, "y": 80, "w": 120, "h": 150}, "score": 0.94,
        "keypoints5": [[130, 140], [190, 138], [160, 170], [135, 200], [185, 198]],
        "quality": {"face_px": 120, "small_face": False, "touches_edge": False, "sharpness": 210.5},
        "crop": {"box": {"x": 76, "y": 50, "w": 168, "h": 210}, "width": 168, "height": 210, "jpeg_base64": CROP},
    }],
    "face_count": 1,
    "parameters": {"min_score": 0.7, "max_faces": 50, "crop_margin": 0.2, "small_face_px": 40},
    "model": {"name": "yunet", "version": "2023mar"},
    "processing_ms": 12.5,
}
NO_FACES = {**DETECT_OK, "status": "no_faces_detected", "faces": [], "face_count": 0}


@pytest.fixture
def inference(frigate):
    """Routes on the shared respx router for the inference host."""
    return frigate


def _post(client, data=JPEG, filename="photo.jpg", ctype="image/jpeg", params="", field="file"):
    return client.post(f"/api/analysis/faces{params}", files={field: (filename, data, ctype)})


def test_requires_login(client, inference):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    assert _post(client).status_code == 401
    assert client.get("/api/models").status_code == 401
    assert not route.called


def test_faces_found(signed_in, inference):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, params="?min_score=0.8&max_faces=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "faces_detected"
    assert body["source"] == "inference" and body["stored"] is False and body["face_count"] == 1
    face = body["faces"][0]
    assert face["box"] == {"x": 100, "y": 80, "w": 120, "h": 150}
    assert face["crop"] == {"box": {"x": 76, "y": 50, "w": 168, "h": 210}, "width": 168, "height": 210,
                            "data_url": f"data:image/jpeg;base64,{CROP}"}
    assert face["quality"]["sharpness"] == 210.5
    assert body["model"] == {"name": "yunet", "version": "2023mar"}
    sent = route.calls.last.request
    assert sent.content == JPEG  # only the file bytes reach the model, not the multipart envelope
    assert sent.url.params["min_score"] == "0.8" and sent.url.params["max_faces"] == "5"


@pytest.mark.parametrize("data, ctype", [(PNG, "image/png"), (WEBP, "image/webp")])
def test_png_and_webp_are_forwarded(signed_in, inference, data, ctype):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)
    assert _post(signed_in, data, "x", ctype).status_code == 200
    assert route.calls.last.request.content == data


def test_no_faces_is_distinct_from_errors(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)
    resp = _post(signed_in, PNG, "blank.png", "image/png")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_faces_detected" and body["faces"] == [] and "error" not in body


def test_client_filename_and_type_are_ignored(signed_in, inference):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)
    resp = _post(signed_in, JPEG, "../../etc/passwd", "text/plain")
    assert resp.status_code == 200  # bytes are a JPEG; name and declared type play no role
    assert "passwd" not in resp.text
    assert route.called


@pytest.mark.parametrize(
    "kwargs, status, code",
    [
        ({"data": b"GIF89a...."}, 415, "unsupported_format"),
        ({"data": b"%PDF-1.7"}, 415, "unsupported_format"),
        ({"data": b""}, 400, "invalid_image"),
        ({"field": "image"}, 400, "invalid_image"),  # wrong field name
    ],
)
def test_upload_validation(signed_in, inference, kwargs, status, code):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, **kwargs)
    assert resp.status_code == status
    assert resp.json()["error"] == code
    assert not route.called  # rejected before reaching the model


def test_large_upload_never_touches_disk(signed_in, inference, monkeypatch):
    """Starlette spools parts over 1 MB to a temp file by default; uploads must stay in memory."""
    import tempfile

    import starlette.formparsers as fp

    created, rolled = [], []

    class Recorder(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(kwargs.get("max_size", args[0] if args else 0))

        def rollover(self):
            rolled.append(True)
            super().rollover()

    monkeypatch.setattr(fp, "SpooledTemporaryFile", Recorder)
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)
    big = b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024)  # 3 MB, under the 10 MB cap
    resp = _post(signed_in, big)
    assert resp.status_code == 200
    assert created, "the multipart parser was not exercised"
    assert rolled == [], "upload was written to a temporary file on disk"
    assert route.calls.last.request.content == big


def test_spool_guard_detects_disk_writes(monkeypatch):
    """Control: with Starlette's default threshold the same recorder does see a rollover."""
    import tempfile

    rolled = []

    class Recorder(tempfile.SpooledTemporaryFile):
        def rollover(self):
            rolled.append(True)
            super().rollover()

    f = Recorder(max_size=1024 * 1024)
    f.write(b"0" * (2 * 1024 * 1024))
    f.close()
    assert rolled == [True]


def test_non_multipart_body_rejected(signed_in, inference):
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = signed_in.post("/api/analysis/faces", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert resp.status_code == 415 and resp.json()["error"] == "unsupported_format"
    assert not route.called


def test_malformed_multipart_rejected(signed_in, inference):
    resp = signed_in.post("/api/analysis/faces", content=b"--x\r\nnonsense",
                          headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert resp.status_code == 400


def test_size_limit(signed_in, inference, settings):
    settings.analysis_max_image_bytes = 1000
    route = inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    resp = _post(signed_in, b"\xff\xd8\xff" + b"0" * 2000)
    assert resp.status_code == 413 and resp.json()["error"] == "too_large"
    big = _post(signed_in, b"\xff\xd8\xff" + b"0" * 200_000)  # rejected from Content-Length / while streaming
    assert big.status_code == 413
    assert not route.called


@pytest.mark.parametrize(
    "status, payload, expect_status, expect_code",
    [
        (400, {"error": "invalid_image", "detail": "The file could not be decoded as an image"}, 400, "invalid_image"),
        (413, {"error": "too_large", "detail": "Image has 9000x9000 pixels"}, 413, "too_large"),
        (415, {"error": "unsupported_format", "detail": "Only JPEG, PNG and WebP"}, 415, "unsupported_format"),
        (503, {"error": "model_unavailable", "detail": "not loaded", "model": "yunet", "model_status": "missing"},
         503, "model_unavailable"),
        (500, {"error": "inference_failed", "detail": "The face detector failed on this image"}, 500, "inference_failed"),
        (500, {"error": "something_internal", "detail": "Traceback /opt/app/x.py"}, 502, "inference_failed"),
    ],
)
def test_inference_errors_are_mapped(signed_in, inference, status, payload, expect_status, expect_code):
    inference.post(f"{INFERENCE}/v1/detect").respond(status, json=payload)
    resp = _post(signed_in)
    assert resp.status_code == expect_status
    body = resp.json()
    assert body["error"] == expect_code
    assert "Traceback" not in resp.text and "/opt/app" not in resp.text
    assert "faces" not in body  # never a result on failure
    if expect_code == "model_unavailable":
        assert body["model_status"] == "missing"


def test_inference_unreachable(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").mock(side_effect=httpx.ConnectError("refused"))
    resp = _post(signed_in)
    assert resp.status_code == 503
    assert resp.json()["error"] == "inference_unavailable"
    assert "faces" not in resp.json()


def test_inference_timeout(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").mock(side_effect=httpx.ReadTimeout("slow"))
    resp = _post(signed_in)
    assert resp.status_code == 503
    assert "did not answer in time" in resp.json()["detail"]


def test_non_json_inference_response(signed_in, inference):
    inference.post(f"{INFERENCE}/v1/detect").respond(200, text="<html>proxy error</html>")
    assert _post(signed_in).status_code == 502


def test_response_contains_no_paths_or_secrets(signed_in, inference, settings):
    inference.post(f"{INFERENCE}/v1/detect").respond(200, json=DETECT_OK)
    text = _post(signed_in).text
    for leak in (str(settings.media_root), settings.database_url, "credentials", "inference.test", "\\", "/data/"):
        assert leak not in text


def test_models_listing(signed_in, inference):
    inference.get(f"{INFERENCE}/v1/models").respond(200, json=[{
        "name": "yunet", "task": "face_detection", "version": "2023mar", "status": "ready", "detail": None,
        "license": "MIT", "license_url": "https://example", "source": "https://example", "notes": "n",
        "runtime": "opencv 4.14.0", "sha256": "abc",
    }])
    body = signed_in.get("/api/models").json()
    assert body["inference"] == {"reachable": True, "ready": True}
    assert body["models"][0]["status"] == "ready" and body["models"][0]["sha256"] == "abc"


def test_models_listing_reports_not_ready(signed_in, inference):
    inference.get(f"{INFERENCE}/v1/health").respond(200, json={"status": "ok", "ready": False,
                                                              "models": {"yunet": "missing"}})
    inference.get(f"{INFERENCE}/v1/models").respond(200, json=[{"name": "yunet", "status": "missing"}])
    body = signed_in.get("/api/models").json()
    assert body["inference"] == {"reachable": True, "ready": False}


def test_models_listing_when_unreachable(signed_in, inference):
    inference.get(f"{INFERENCE}/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    body = signed_in.get("/api/models").json()
    assert body["inference"]["reachable"] is False and body["models"] == []


def test_health_reports_inference(client, inference):
    assert client.get("/api/health").json()["inference"] == {"reachable": True, "models_ready": True}
    inference.get(f"{INFERENCE}/v1/health").respond(200, json={"status": "ok", "ready": False,
                                                              "models": {"yunet": "load_error"}})
    assert client.get("/api/health").json()["inference"]["models_ready"] is False
    inference.get(f"{INFERENCE}/v1/health").mock(side_effect=httpx.ConnectError("refused"))
    assert client.get("/api/health").json()["inference"]["reachable"] is False
