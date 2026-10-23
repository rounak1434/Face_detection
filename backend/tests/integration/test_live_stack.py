"""Real integration tests: no mocks. They talk to the running docker stack
(Caddy -> api -> Frigate -> test stream or NVR) and are skipped unless
IT_BASE_URL is set. On the Linux host, run them in a container with
deploy/run-integration-tests.sh (docs/SETUP.md section 7), or directly:

    IT_BASE_URL=https://<SITE_ADDRESS> IT_VERIFY_TLS=0 IT_ADMIN_USER=admin \
    IT_ADMIN_PASSWORD='...' IT_CAMERA=entrance pytest -m integration -v
"""

import json
import os
import ssl
import time
from urllib.parse import urlsplit

import httpx
import pytest
from websockets.sync.client import connect

pytestmark = pytest.mark.integration

BASE = os.environ.get("IT_BASE_URL", "")
VERIFY = os.environ.get("IT_VERIFY_TLS", "1") != "0"


@pytest.fixture(scope="module")
def api():
    if not BASE:
        pytest.skip("IT_BASE_URL not set")
    client = httpx.Client(base_url=BASE, verify=VERIFY, timeout=30)
    resp = client.post(
        "/api/auth/login",
        json={"username": os.environ["IT_ADMIN_USER"], "password": os.environ["IT_ADMIN_PASSWORD"]},
    )
    assert resp.status_code == 200, f"login failed: HTTP {resp.status_code} {resp.text[:200]}"
    yield client
    client.close()


@pytest.fixture(scope="module")
def camera():
    return os.environ.get("IT_CAMERA", "entrance")


def test_engine_reachable_and_version(api):
    frigate = api.get("/api/health").json()["frigate"]
    assert frigate["reachable"], frigate
    assert frigate["version"].startswith("0.18"), frigate


def test_camera_listed(api, camera):
    names = [c["name"] for c in api.get("/api/cameras").json()]
    assert camera in names, names


def test_live_frame_is_real_jpeg(api, camera):
    resp = api.get(f"/api/cameras/{camera}/latest.jpg")
    assert resp.status_code == 200
    assert resp.content[:3] == b"\xff\xd8\xff"
    assert resp.headers.get("x-camera-offline") != "true", "engine served an offline fallback frame"


def test_snapshot_capture(api, camera):
    resp = api.post(f"/api/cameras/{camera}/snapshot")
    assert resp.status_code == 201, resp.text
    capture = resp.json()
    assert api.get(capture["file_url"]).content[:3] == b"\xff\xd8\xff"


def test_live_websocket_delivers_video(api, camera):
    """Exercises the real go2rtc MSE handshake through the api relay."""
    parts = urlsplit(BASE)
    scheme = "wss" if parts.scheme == "https" else "ws"
    ctx = None
    if scheme == "wss":
        ctx = ssl.create_default_context()
        if not VERIFY:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    cookie = "; ".join(f"{k}={v}" for k, v in api.cookies.items())
    url = f"{scheme}://{parts.netloc}/api/cameras/{camera}/live"
    with connect(url, ssl=ctx, additional_headers={"Cookie": cookie}, origin=f"{parts.scheme}://{parts.netloc}",
                 open_timeout=15, max_size=None) as ws:
        ws.send(json.dumps({"type": "mse", "value": "avc1.640029,avc1.64002A,avc1.640033,hvc1.1.6.L153.B0,mp4a.40.2"}))
        first = json.loads(ws.recv(timeout=15))
        assert first.get("type") == "mse", first
        assert first["value"].startswith("video/mp4"), first
        data = ws.recv(timeout=15)
        assert isinstance(data, bytes) and len(data) > 0


def test_synthetic_source_connection_test(api):
    """Real ffprobe inside the api container against the real test stream."""
    device = api.post("/api/devices/synthetic").json()
    time.sleep(6)  # respect the per-device test cooldown if the suite is re-run quickly
    result = api.post(f"/api/devices/{device['id']}/test")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "connected", body
    assert body["streams"]["main"]["codec"] == "h264"


def _tiny_png(width=64, height=48) -> bytes:
    """A plain grey PNG built without image libraries (no face in it)."""
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_face_model_ready(api):
    body = api.get("/api/models").json()
    assert body["inference"]["reachable"], body
    yunet = next(m for m in body["models"] if m["name"] == "yunet")
    assert yunet["status"] == "ready", yunet


def test_face_analysis_no_face(api):
    resp = api.post("/api/analysis/faces", content=_tiny_png(), headers={"Content-Type": "image/png"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "inference" and body["model"]["name"] == "yunet"
    assert body["faces"] == [] and body["message"] == "No face detected"


def test_face_analysis_rejects_non_image(api):
    resp = api.post("/api/analysis/faces", content=b"GIF89a....", headers={"Content-Type": "image/png"})
    assert resp.status_code == 415


@pytest.mark.skipif(not os.environ.get("IT_FACE_IMAGE"), reason="IT_FACE_IMAGE not set")
def test_face_analysis_real_face(api):
    path = os.environ["IT_FACE_IMAGE"]
    ctype = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as fh:
        resp = api.post("/api/analysis/faces", content=fh.read(), headers={"Content-Type": ctype})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["face_count"] >= 1
    assert body["faces"][0]["crop_data_url"].startswith("data:image/jpeg;base64,")


def test_clip_capture(api, camera):
    resp = api.post(f"/api/cameras/{camera}/clip", json={"seconds": 10})
    assert resp.status_code == 202, resp.text
    capture = resp.json()
    deadline = time.monotonic() + 240
    while capture["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(3)
        capture = api.get(f"/api/captures/{capture['id']}").json()
    assert capture["status"] == "ready", capture
    head = api.get(capture["file_url"], headers={"Range": "bytes=0-11"})
    assert head.status_code == 206
    assert head.content[4:8] == b"ftyp", "not an MP4 file"
