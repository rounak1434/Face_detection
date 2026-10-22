import time

import httpx
import pytest

from ai_surveillance.media import MediaStore

from .conftest import JPEG

MP4 = b"\x00\x00\x00\x18ftypmp42fake-mp4-body"


def test_health_reports_frigate(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["app"] == "AI Surveillance"
    assert body["frigate"] == {"reachable": True, "version": "0.18.2-test"}


def test_health_when_frigate_down(client, frigate):
    frigate.get("/api/version").respond(503)
    assert client.get("/api/health").json()["frigate"]["reachable"] is False


def test_camera_list_hides_stream_urls(signed_in):
    resp = signed_in.get("/api/cameras")
    assert resp.status_code == 200
    assert resp.json() == [
        {"name": "entrance", "enabled": True, "detect_width": 1280, "detect_height": 720, "recording": True}
    ]
    assert "secret" not in resp.text
    assert "rtsp://" not in resp.text


def test_unknown_camera_rejected(signed_in):
    assert signed_in.post("/api/cameras/backdoor/snapshot").status_code == 404
    assert signed_in.get("/api/cameras/..%2Fetc/latest.jpg").status_code == 404


def test_snapshot_is_stored_and_served(signed_in):
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 201
    capture = resp.json()
    assert capture["status"] == "ready"
    assert capture["size_bytes"] == len(JPEG)
    file_resp = signed_in.get(capture["file_url"])
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "image/jpeg"
    assert file_resp.content == JPEG
    listed = signed_in.get("/api/captures").json()
    assert [c["id"] for c in listed] == [capture["id"]]


def test_snapshot_failure_reported(signed_in, frigate):
    frigate.get("/api/entrance/latest.jpg").respond(500)
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 502
    assert signed_in.get("/api/captures").json()[0]["status"] == "failed"


def _wait_for(client, capture_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        capture = client.get(f"/api/captures/{capture_id}").json()
        if capture["status"] != "pending":
            return capture
        time.sleep(0.05)
    raise AssertionError("capture stayed pending")


def test_clip_export_flow(signed_in, frigate):
    export_route = frigate.post(url__regex=r"/api/export/entrance/start/\d+/end/\d+").respond(
        200, json={"success": True, "message": "Starting export", "export_id": "entrance_abc"}
    )
    status_route = frigate.get("/api/exports/entrance_abc")
    status_route.side_effect = [
        _json(200, {"id": "entrance_abc", "in_progress": True, "video_path": ""}),
        _json(200, {"id": "entrance_abc", "in_progress": False,
                    "video_path": "/media/frigate/exports/entrance_abc.mp4"}),
    ]
    frigate.get("/exports/entrance_abc.mp4").respond(200, content=MP4)

    resp = signed_in.post("/api/cameras/entrance/clip", json={"seconds": 10})
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"

    capture = _wait_for(signed_in, resp.json()["id"])
    assert capture["status"] == "ready", capture["error"]
    assert capture["duration_seconds"] == 10
    start, end = map(int, export_route.calls.last.request.url.path.split("/")[-3::2])
    assert end - start == 10
    file_resp = signed_in.get(capture["file_url"])
    assert file_resp.headers["content-type"] == "video/mp4"
    assert file_resp.content == MP4


def test_clip_export_rejected_by_frigate(signed_in, frigate):
    frigate.post(url__regex=r"/api/export/.*").respond(
        200, json={"success": False, "message": "No recordings found for time range"}
    )
    resp = signed_in.post("/api/cameras/entrance/clip", json={})
    capture = _wait_for(signed_in, resp.json()["id"])
    assert capture["status"] == "failed"
    assert "No recordings" in capture["error"]
    assert capture["file_url"] is None


@pytest.mark.parametrize("seconds", [0, 61, -5])
def test_clip_length_limits(signed_in, seconds):
    resp = signed_in.post("/api/cameras/entrance/clip", json={"seconds": seconds})
    assert resp.status_code == 422


@pytest.mark.parametrize("name", ["../secret.jpg", "a.jpg", "0" * 32 + ".exe", "/etc/passwd", "0" * 32 + ".jpg/../x"])
def test_media_store_rejects_unsafe_names(tmp_path, name):
    store = MediaStore(tmp_path)
    with pytest.raises(ValueError):
        store.path_for(name)


def test_media_store_accepts_generated_names(tmp_path):
    store = MediaStore(tmp_path)
    name = "a" * 32 + ".jpg"
    assert store.write_atomic(name, b"x") == 1
    assert store.path_for(name).read_bytes() == b"x"


def _json(code, body):
    return httpx.Response(code, json=body)
