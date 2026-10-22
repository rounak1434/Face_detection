"""Live-stack checks. Run on the Linux host with the compose stack up:

    IT_BASE_URL=https://localhost IT_ADMIN_USER=admin IT_ADMIN_PASSWORD=... \
    IT_CAMERA=entrance pytest -m integration

IT_VERIFY_TLS=0 accepts Caddy's local CA without installing it.
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api():
    base = os.environ.get("IT_BASE_URL")
    if not base:
        pytest.skip("IT_BASE_URL not set")
    client = httpx.Client(base_url=base, verify=os.environ.get("IT_VERIFY_TLS", "1") != "0", timeout=30)
    resp = client.post(
        "/api/auth/login",
        json={"username": os.environ["IT_ADMIN_USER"], "password": os.environ["IT_ADMIN_PASSWORD"]},
    )
    resp.raise_for_status()
    yield client
    client.close()


@pytest.fixture(scope="module")
def camera():
    return os.environ.get("IT_CAMERA", "entrance")


def test_frigate_reachable(api):
    frigate = api.get("/api/health").json()["frigate"]
    assert frigate["reachable"], frigate


def test_camera_listed(api, camera):
    assert camera in [c["name"] for c in api.get("/api/cameras").json()]


def test_live_frame_is_jpeg(api, camera):
    resp = api.get(f"/api/cameras/{camera}/latest.jpg")
    assert resp.status_code == 200
    assert resp.content[:3] == b"\xff\xd8\xff"


def test_snapshot_capture(api, camera):
    capture = api.post(f"/api/cameras/{camera}/snapshot").json()
    assert capture["status"] == "ready"
    assert api.get(capture["file_url"]).content[:3] == b"\xff\xd8\xff"


def test_clip_capture(api, camera):
    capture = api.post(f"/api/cameras/{camera}/clip", json={"seconds": 10}).json()
    deadline = time.monotonic() + 240
    while capture["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(3)
        capture = api.get(f"/api/captures/{capture['id']}").json()
    assert capture["status"] == "ready", capture
    head = api.get(capture["file_url"]).content[:12]
    assert head[4:8] == b"ftyp", "not an MP4 file"
