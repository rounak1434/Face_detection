import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_surveillance.main import create_app
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


def _disk_full(path):
    return OSError(28, "No space left on device", str(path))


def test_snapshot_disk_error_is_reported_without_paths(signed_in, settings, monkeypatch):
    real_write_bytes = Path.write_bytes

    def fill_disk(self, data):
        if self.name.endswith(".part"):
            real_write_bytes(self, data[:3])  # partly written, then the disk is full
            raise _disk_full(self)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", fill_disk)
    resp = signed_in.post("/api/cameras/entrance/snapshot")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "No space left on device" in detail
    assert str(settings.media_root) not in detail and "captures" not in detail
    assert list((settings.media_root / "captures").iterdir()) == []  # no partial file left
    listed = signed_in.get("/api/captures").json()
    assert listed[0]["status"] == "failed"
    assert str(settings.media_root) not in listed[0]["error"]


def _ready_export(frigate):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "success"})
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(200, content=MP4)


def test_clip_disk_error_is_reported_without_paths(signed_in, frigate, settings, monkeypatch):
    _ready_export(frigate)
    monkeypatch.setattr(MediaStore, "finalize", lambda self, name: (_ for _ in ()).throw(_disk_full(self.path_for(name))))
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "No space left on device" in capture["error"]
    assert str(settings.media_root) not in capture["error"] and "captures" not in capture["error"]
    assert list((settings.media_root / "captures").iterdir()) == []


def test_clip_is_not_left_pending_when_cleanup_also_fails(signed_in, frigate, settings, monkeypatch):
    _ready_export(frigate)
    monkeypatch.setattr(MediaStore, "finalize", lambda self, name: (_ for _ in ()).throw(_disk_full(self.path_for(name))))
    monkeypatch.setattr(MediaStore, "delete", lambda self, name: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert "No space left on device" in capture["error"]


def test_startup_removes_partial_files_left_by_an_interrupted_capture(settings, frigate):
    captures = settings.media_root / "captures"
    captures.mkdir(parents=True)
    kept = captures / ("a" * 32 + ".jpg")
    kept.write_bytes(JPEG)
    for name in ("b" * 32 + ".mp4.part", "c" * 32 + ".jpg.part"):
        (captures / name).write_bytes(b"partial")
    other = captures / "notes.txt"  # not ours: never touched
    other.write_bytes(b"x")
    with TestClient(create_app(settings)):
        pass
    assert sorted(p.name for p in captures.iterdir()) == sorted([kept.name, other.name])


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_frigate_export_is_deleted_once_the_clip_is_stored(signed_in, frigate):
    _ready_export(frigate)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "ready"
    route = frigate.routes["delete_exports"]
    assert _wait_until(lambda: route.called)
    assert json.loads(route.calls.last.request.content) == {"ids": ["entrance_abc"]}
    assert signed_in.get(capture["file_url"]).content == MP4  # our copy is intact


@pytest.mark.parametrize("status,body", [
    (400, {"success": False, "message": "Can not delete in-progress export."}),
    (403, {"success": False, "message": "Forbidden"}),
    (500, {"detail": "boom"}),
])
def test_failed_frigate_export_cleanup_keeps_the_clip(signed_in, frigate, status, body):
    _ready_export(frigate)
    frigate.routes["delete_exports"].respond(status, json=body)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert _wait_until(lambda: frigate.routes["delete_exports"].called)
    assert signed_in.get(f"/api/captures/{capture['id']}").json()["status"] == "ready"
    assert signed_in.get(capture["file_url"]).content == MP4


def test_frigate_export_kept_when_configured(signed_in, frigate, settings):
    settings.clip_keep_frigate_export = True
    _ready_export(frigate)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "ready"
    time.sleep(0.1)
    assert not frigate.routes["delete_exports"].called


def test_frigate_export_of_a_failed_clip_is_cleaned_up(signed_in, frigate):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "success"})
    frigate.get("/api/exports/entrance_abc").respond(200, json=EXPORT_ROW)
    frigate.get("/exports/entrance_abc.mp4").respond(403)  # download fails after Frigate exported
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    assert _wait_until(lambda: frigate.routes["delete_exports"].called)


def test_no_cleanup_when_frigate_never_created_an_export(signed_in, frigate):
    frigate.post(url__regex=r"/api/export/.*").respond(400, json={"success": False, "message": "No recordings found"})
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    assert capture["status"] == "failed"
    time.sleep(0.1)
    assert not frigate.routes["delete_exports"].called


def test_deleting_a_clip_also_removes_its_frigate_export(signed_in, frigate, settings):
    settings.clip_keep_frigate_export = True  # e.g. an older clip whose export was kept
    _ready_export(frigate)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    settings.clip_keep_frigate_export = False
    assert signed_in.delete(f"/api/captures/{capture['id']}").status_code == 204
    route = frigate.routes["delete_exports"]
    assert _wait_until(lambda: route.called)
    assert json.loads(route.calls.last.request.content) == {"ids": ["entrance_abc"]}


def test_deleting_a_clip_succeeds_when_frigate_is_unreachable(signed_in, frigate, settings):
    settings.clip_keep_frigate_export = True
    _ready_export(frigate)
    capture = _wait_for(signed_in, signed_in.post("/api/cameras/entrance/clip", json={}).json()["id"])
    settings.clip_keep_frigate_export = False
    frigate.routes["delete_exports"].side_effect = httpx.ConnectError("refused")
    assert signed_in.delete(f"/api/captures/{capture['id']}").status_code == 204
    assert signed_in.get(f"/api/captures/{capture['id']}").status_code == 404


def test_delete_snapshot_removes_row_and_file(signed_in, settings):
    capture = signed_in.post("/api/cameras/entrance/snapshot").json()
    stored = settings.media_root / "captures" / f"{capture['id']}.jpg"
    assert stored.is_file()
    resp = signed_in.delete(f"/api/captures/{capture['id']}")
    assert resp.status_code == 204
    assert not stored.exists()
    assert signed_in.get(f"/api/captures/{capture['id']}").status_code == 404
    assert signed_in.get(capture["file_url"]).status_code == 404
    assert signed_in.get("/api/captures").json() == []


def test_delete_failed_capture(signed_in, frigate):
    frigate.get("/api/entrance/latest.jpg").respond(500, json={"message": "Unable to get valid frame"})
    signed_in.post("/api/cameras/entrance/snapshot")
    failed = signed_in.get("/api/captures").json()[0]
    assert failed["status"] == "failed"
    assert signed_in.delete(f"/api/captures/{failed['id']}").status_code == 204
    assert signed_in.get("/api/captures").json() == []


def test_delete_unknown_capture_is_404(signed_in):
    assert signed_in.delete("/api/captures/does-not-exist").status_code == 404


def test_delete_requires_sign_in(client, signed_in):
    capture = signed_in.post("/api/cameras/entrance/snapshot").json()
    client.cookies.clear()
    assert client.delete(f"/api/captures/{capture['id']}").status_code == 401


def test_delete_refused_while_clip_is_pending(signed_in, frigate):
    _queue_export(frigate)
    frigate.get("/api/jobs/export/entrance_abc").respond(200, json={"status": "running"})
    capture = signed_in.post("/api/cameras/entrance/clip", json={}).json()
    resp = signed_in.delete(f"/api/captures/{capture['id']}")
    assert resp.status_code == 409
    assert signed_in.get(f"/api/captures/{capture['id']}").status_code == 200


def test_delete_keeps_the_record_when_the_file_cannot_be_removed(signed_in, settings, monkeypatch):
    capture = signed_in.post("/api/cameras/entrance/snapshot").json()
    stored = settings.media_root / "captures" / f"{capture['id']}.jpg"

    def refuse(self, missing_ok=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "unlink", refuse)
    resp = signed_in.delete(f"/api/captures/{capture['id']}")
    monkeypatch.undo()
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "Permission denied" in detail and str(settings.media_root) not in detail
    assert stored.is_file()  # nothing lost, nothing orphaned: the record still points at it
    assert signed_in.get(f"/api/captures/{capture['id']}").json()["status"] == "ready"


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
