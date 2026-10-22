import json
import time
from datetime import datetime

import httpx
import pytest

from ai_surveillance.media import MediaStore

from .conftest import JPEG

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"fake-mp4-body" * 200


def test_health_reports_frigate(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["app"] == "AI Surveillance"
    assert body["frigate"] == {"reachable": True, "version": "0.18.2-test"}


def test_health_when_frigate_down(client, frigate):
    frigate.get("/api/version").mock(side_effect=httpx.ConnectError("refused"))
    body = client.get("/api/health").json()
    assert body["frigate"] == {"reachable": False, "error": "Frigate request failed: ConnectError"}


def test_camera_list_hides_stream_urls(signed_in):
    resp = signed_in.get("/api/cameras")
    assert resp.status_code == 200
    assert resp.json() == [
        {"name": "entrance", "enabled": True, "detect_width": 1280, "detect_height": 720, "recording": True}
    ]
    assert "rtsp://" not in resp.text


def test_unknown_camera_rejected(signed_in):
    assert signed_in.post("/api/cameras/backdoor/snapshot").status_code == 404
    assert signed_in.get("/api/cameras/..%2Fetc/latest.jpg").status_code == 404


def test_latest_frame_uses_frigate_height_param(signed_in, frigate):
    resp = signed_in.get("/api/cameras/entrance/latest.jpg")
    assert resp.status_code == 200
    assert resp.content == JPEG
    # Frigate 0.18 names the parameter "height"; "h" would be ignored.
    params = frigate.routes["latest"].calls.last.request.url.params
    assert params["height"] == "480"
    assert "h" not in params


def test_latest_frame_flags_offline(signed_in, frigate):
    frigate.get("/api/entrance/latest.jpg").respond(
        200, content=JPEG, headers={"content-type": "image/jpeg", "x-frigate-offline": "true"}
    )
    resp = signed_in.get("/api/cameras/entrance/latest.jpg")
    assert resp.headers["x-camera-offline"] == "true"


def test_snapshot_is_stored_and_served(signed_in):
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 201
    capture = resp.json()
    assert capture["status"] == "ready"
    assert capture["size_bytes"] == len(JPEG)
    assert datetime.fromisoformat(capture["created_at"]).tzinfo is not None
    file_resp = signed_in.get(capture["file_url"])
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "image/jpeg"
    assert file_resp.content == JPEG
    listed = signed_in.get("/api/captures").json()
    assert [c["id"] for c in listed] == [capture["id"]]
    assert datetime.fromisoformat(listed[0]["created_at"]).utcoffset().total_seconds() == 0


def test_snapshot_failure_reported(signed_in, frigate):
    frigate.get("/api/entrance/latest.jpg").respond(500, json={"success": False, "message": "Unable to get valid frame"})
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 502
    assert "Unable to get valid frame" in resp.json()["detail"]
    assert signed_in.get("/api/captures").json()[0]["status"] == "failed"


def test_snapshot_refused_when_camera_receives_no_frames(signed_in, frigate):
    # Frigate would still answer latest.jpg with a placeholder image.
    frigate.get("/api/stats").respond(200, json={"cameras": {"entrance": {"camera_fps": 0.0}}})
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 502
    assert "offline" in resp.json()["detail"]


def test_snapshot_refused_for_offline_fallback_frame(signed_in, frigate):
    frigate.get("/api/entrance/latest.jpg").respond(
        200, content=JPEG, headers={"content-type": "image/jpeg", "x-frigate-offline": "true"}
    )
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 502
    assert "fallback frame" in resp.json()["detail"]


def _wait_for(client, capture_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        capture = client.get(f"/api/captures/{capture_id}").json()
        if capture["status"] != "pending":
            return capture
        time.sleep(0.05)
    raise AssertionError("capture stayed pending")


def _queue_export(frigate, export_id="entrance_abc"):
    # Frigate 0.18: 202 Accepted with a queued job, not a finished export.
    return frigate.post(url__regex=r"/api/export/entrance/start/\d+/end/\d+").respond(
        202, json={"success": True, "message": "Export queued.", "export_id": export_id, "status": "queued"}
    )


def _job(status, **extra):
    return httpx.Response(200, json={"id": "entrance_abc", "job_type": "export", "status": status, **extra})


EXPORT_ROW = {"id": "entrance_abc", "camera": "entrance", "in_progress": False,
              "video_path": "/media/frigate/exports/entrance_abc.mp4"}


def test_clip_export_flow(signed_in, frigate):
    export_route = _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").side_effect = [
        _job("queued"), _job("running"), _job("success", results={"video_path": EXPORT_ROW["video_path"]}),
    ]
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(200, content=MP4)

    resp = signed_in.post("/api/cameras/entrance/clip", json={"seconds": 10})
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"

    capture = _wait_for(signed_in, resp.json()["id"])
    assert capture["status"] == "ready", capture["error"]
    assert capture["duration_seconds"] == 10
    request = export_route.calls.last.request
    start, end = map(int, request.url.path.split("/")[-3::2])
    assert end - start == 10
    assert json.loads(request.content) == {"source": "recordings", "name": f"capture-{capture['id']}"}

    file_resp = signed_in.get(capture["file_url"])
    assert file_resp.headers["content-type"] == "video/mp4"
    assert file_resp.content == MP4


def test_clip_file_supports_range_requests(signed_in, frigate):
    # Browsers need byte ranges to seek in <video>.
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "success"})
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(200, content=MP4)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    resp = signed_in.get(capture["file_url"], headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.content == MP4[:100]


def test_clip_succeeds_after_frigate_forgets_finished_job(signed_in, frigate):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(404, json={"success": False, "message": "Job not found"})
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(200, content=MP4)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "ready", capture["error"]


def test_clip_job_failure_reason_is_kept(signed_in, frigate):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(
        200, json={"status": "failed", "error_message": "Export did not complete"}
    )
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "Export did not complete" in capture["error"]


def test_clip_rejected_when_no_recordings(signed_in, frigate):
    frigate.post(url__regex=r"/api/export/.*").respond(
        400, json={"success": False, "message": "No recordings found for time range"}
    )
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "No recordings found for time range" in capture["error"]
    assert capture["file_url"] is None


def test_clip_times_out(signed_in, frigate, settings):
    settings.clip_export_timeout_seconds = 0.2
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "running"})
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "Timed out" in capture["error"]


def test_clip_download_failure_leaves_no_file(signed_in, frigate, settings):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "success"})
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(403)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert list((settings.media_root / "captures").iterdir()) == []


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
