"""Face events: ingestion, deduplication, cursor, retries, files, retention (ADR-0004, Phase 3).

Mocked Frigate (respx, v0.18.2 shapes) and mocked inference; a real temporary SQLite
database with the real migrations; real files in a temporary media directory.
"""

import asyncio
import base64
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ai_surveillance import face_events as fe
from ai_surveillance.db import Database, FaceDetection, FaceEvent
from ai_surveillance.frigate_client import FrigateClient
from ai_surveillance.inference_client import InferenceClient
from ai_surveillance.media import MediaStore

from .conftest import FRIGATE, INFERENCE, JPEG

CROP = b"\xff\xd8\xff\xe0crop\xff\xd9"
FACE = {"index": 0, "box": {"x": 10, "y": 20, "w": 80, "h": 100}, "score": 0.93,
        "keypoints5": [[30, 50], [60, 50], [45, 70], [35, 90], [55, 90]],
        "quality": {"face_px": 80, "small_face": False, "touches_edge": False, "sharpness": 120.0},
        "crop": {"box": {"x": 0, "y": 0, "w": 96, "h": 120}, "width": 96, "height": 120,
                 "jpeg_base64": base64.b64encode(CROP).decode()}}
NOW = time.time()


def event(n: int, start: float | None = None) -> dict:
    start = NOW - 600 + n if start is None else start
    return {"id": f"{start:.6f}-ev{n:04d}", "label": "person", "camera": "entrance", "start_time": start,
            "end_time": start + 5, "has_snapshot": True, "data": {}}


def detect_result(faces):
    return {"status": "x", "image": {"width": 1280, "height": 720, "format": "image/jpeg"}, "faces": faces,
            "face_count": len(faces), "model": {"name": "yunet", "version": "2023mar"}, "processing_ms": 5.0}


class FakeFrigate:
    """Serves /api/events like Frigate 0.18.2: start time > after, oldest first, limit/offset."""

    def __init__(self, router):
        self.events: list[dict] = []
        self.requests: list[dict] = []
        self.route = router.get(f"{FRIGATE}/api/events")
        self.route.mock(side_effect=self.list_events)
        router.get(url__regex=rf"{FRIGATE}/api/events/[^/]+/snapshot\.jpg").respond(
            200, content=JPEG, headers={"content-type": "image/jpeg"})

    def list_events(self, request):
        params = dict(request.url.params)
        self.requests.append(params)
        after, limit, offset = float(params["after"]), int(params["limit"]), int(params.get("offset", 0))
        matching = sorted((e for e in self.events if e["start_time"] > after), key=lambda e: e["start_time"])
        return httpx.Response(200, json=matching[offset:offset + limit])


@pytest.fixture
def env(settings, tmp_path):
    settings.face_frame_source = "snapshot"
    db = Database(settings.database_url)
    db.migrate()
    store = MediaStore(tmp_path / "media", "faces")
    with respx.mock(assert_all_called=False) as router:
        frigate = FakeFrigate(router)
        detect = router.post(f"{INFERENCE}/v1/detect", name="detect")
        detect.respond(200, json=detect_result([FACE]))
        yield {"settings": settings, "db": db, "store": store, "frigate": frigate, "detect": detect,
               "ingestor": new_ingestor(settings, db, store)}
    db.engine.dispose()


def new_ingestor(settings, db, store):
    return fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), db.sessionmaker, store)


def ingest(env, ingestor=None):
    return asyncio.run((ingestor or env["ingestor"]).ingest_once())


def rows(env):
    with env["db"].sessionmaker() as s:
        return s.query(FaceEvent).order_by(FaceEvent.started_at).all(), s.query(FaceDetection).all()


def files(env):
    return sorted(p.name for p in env["store"].captures_dir.iterdir())


def test_event_with_faces_is_stored_with_frame_crops_and_detections(env):
    env["frigate"].events = [event(1)]
    stats = ingest(env)
    assert stats.stored == 1 and stats.stopped is None
    (ev,), (det,) = rows(env)
    assert ev.frigate_event_id == event(1)["id"] and ev.camera == "entrance" and ev.status == "faces_detected"
    assert ev.face_count == 1 and ev.frame_source == "snapshot" and ev.detector == "yunet 2023mar"
    assert (ev.frame_width, ev.frame_height) == (1280, 720)
    assert abs(ev.started_at.replace(tzinfo=UTC).timestamp() - event(1)["start_time"]) < 0.001
    assert (det.box_x, det.box_y, det.box_w, det.box_h, det.score, det.position) == (10, 20, 80, 100, 0.93, 0)
    assert json.loads(det.keypoints) == FACE["keypoints5"] and json.loads(det.quality)["face_px"] == 80
    assert (env["store"].captures_dir / ev.frame_file).read_bytes() == JPEG
    assert (env["store"].captures_dir / det.crop_file).read_bytes() == CROP
    assert files(env) == sorted([ev.frame_file, det.crop_file])


def test_event_without_faces_is_recorded_without_images(env):
    env["frigate"].events = [event(1)]
    env["detect"].respond(200, json=detect_result([]))
    ingest(env)
    (ev,), dets = rows(env)
    assert ev.status == "no_faces_detected" and ev.face_count == 0 and ev.frame_file is None and dets == []
    assert files(env) == []  # no frame of a person is kept when no face was found


def test_each_frigate_event_is_stored_once_across_polls_and_restarts(env):
    env["frigate"].events = [event(1), event(2)]
    assert ingest(env).stored == 2
    again = ingest(env)
    assert again.stored == 0 and again.known == 2
    restarted = new_ingestor(env["settings"], env["db"], env["store"])
    assert ingest(env, restarted).stored == 0
    assert len(rows(env)[0]) == 2 and env["detect"].call_count == 2  # never analysed twice


def test_cursor_starts_at_the_backfill_window_then_follows_the_newest_event_minus_lookback(env):
    env["settings"].face_backfill_seconds = 7200
    env["settings"].face_event_lookback_seconds = 900
    env["frigate"].events = [event(1)]
    ingest(env)
    first = float(env["frigate"].requests[0]["after"])
    assert abs(first - (time.time() - 7200)) < 5
    ingest(env)
    assert float(env["frigate"].requests[-1]["after"]) == pytest.approx(event(1)["start_time"] - 900, abs=0.01)


def test_a_long_event_that_ends_later_is_still_picked_up_within_the_lookback(env):
    env["frigate"].events = [event(10)]
    ingest(env)
    env["frigate"].events.append(event(5))  # started earlier, appeared only after it ended
    assert ingest(env).stored == 1 and len(rows(env)[0]) == 2


def test_pages_beyond_the_first_are_read(env):
    env["frigate"].events = [event(n) for n in range(60)]
    assert ingest(env).stored == 60
    offsets = [r.get("offset", "0") for r in env["frigate"].requests]
    assert offsets == ["0", "50"]
    # A full first page of known events must not hide newer ones.
    env["frigate"].events.append(event(70))
    assert ingest(env).stored == 1


def test_a_temporary_failure_stops_the_poll_so_later_events_wait(env):
    env["frigate"].events = [event(1), event(2), event(3)]
    env["detect"].mock(side_effect=[httpx.Response(200, json=detect_result([FACE])), httpx.ConnectError("down")])
    stats = ingest(env)
    assert stats.stored == 1 and stats.stopped == "temporary_failure"
    assert [e.frigate_event_id for e in rows(env)[0]] == [event(1)["id"]]
    env["detect"].mock(return_value=httpx.Response(200, json=detect_result([FACE])))
    assert ingest(env).stored == 2  # event 2, then 3, in order


def test_an_event_that_keeps_failing_is_recorded_as_gave_up_after_the_limit(env):
    env["settings"].face_max_attempts = 3
    env["frigate"].events = [event(1), event(2)]
    env["detect"].mock(side_effect=lambda request: httpx.Response(
        503, json={"error": "model_unavailable", "detail": "missing"}))
    for _ in range(2):
        assert ingest(env).stopped == "temporary_failure"
    assert rows(env)[0] == []
    ingest(env)  # third try for event 1: given up; event 2 then fails its first try
    (ev,), _ = rows(env)
    assert ev.status == "gave_up" and "after 3 tries" in ev.detail and ev.frame_file is None


def test_frigate_down_stores_nothing_and_does_not_crash(env):
    env["frigate"].events = [event(1)]
    env["frigate"].route.mock(side_effect=httpx.ConnectError("refused"))
    stats = ingest(env)
    assert stats.stopped == "frigate_unavailable" and rows(env)[0] == []
    env["frigate"].route.mock(side_effect=env["frigate"].list_events)  # Frigate is back
    assert ingest(env).stored == 1


def test_low_disk_pauses_before_analysing(env, monkeypatch):
    env["settings"].capture_min_free_mb = 1024
    monkeypatch.setattr(MediaStore, "free_bytes", lambda self: 10 * 1024 * 1024)
    env["frigate"].events = [event(1)]
    stats = ingest(env)
    assert stats.stopped == "insufficient_storage" and rows(env)[0] == [] and not env["detect"].called


def test_a_failed_image_write_leaves_no_files_and_no_rows(env, monkeypatch):
    env["frigate"].events = [event(1)]
    real = MediaStore.write_atomic
    calls = {"n": 0}

    def flaky(self, name, data):
        calls["n"] += 1
        if calls["n"] == 2:  # the crop, after the frame was written
            raise OSError(28, "No space left on device")
        return real(self, name, data)

    monkeypatch.setattr(MediaStore, "write_atomic", flaky)
    stats = ingest(env)
    assert stats.stopped == "storage_error" and rows(env)[0] == [] and files(env) == []


def test_saving_the_same_event_twice_keeps_one_row_and_removes_the_second_files(env):
    env["frigate"].events = [event(1)]
    ingest(env)
    before = files(env)
    result = asyncio.run(fe.detect_event_faces(
        fe.PersonEvent(event(1)["id"], "entrance", event(1)["start_time"], event(1)["end_time"]),
        env["ingestor"].frigate, env["ingestor"].inference, env["settings"]))
    assert env["ingestor"].save(result) is False
    assert files(env) == before and len(rows(env)[0]) == 1


def test_retention_removes_old_events_with_their_files(env):
    env["settings"].face_backfill_seconds = 50 * 86400
    env["frigate"].events = [event(1, start=NOW - 40 * 86400), event(2)]
    ingest(env)
    old, new = rows(env)[0]
    old_files = [old.frame_file]
    assert fe.purge_old_face_events(env["db"].sessionmaker, env["store"], days=30) == 1
    remaining, dets = rows(env)
    assert [e.id for e in remaining] == [new.id] and all(d.event_id == new.id for d in dets)
    assert not any((env["store"].captures_dir / f).exists() for f in old_files)


def test_retention_keeps_an_event_whose_file_cannot_be_removed(env, monkeypatch):
    env["settings"].face_backfill_seconds = 50 * 86400
    env["frigate"].events = [event(1, start=NOW - 40 * 86400)]
    ingest(env)

    def refuse(self, name):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(MediaStore, "delete", refuse)
    assert fe.purge_old_face_events(env["db"].sessionmaker, env["store"], days=30) == 0
    assert len(rows(env)[0]) == 1  # retried at the next check, nothing orphaned


def test_delete_face_event(env):
    env["frigate"].events = [event(1)]
    ingest(env)
    (ev,), _ = rows(env)
    assert fe.delete_face_event(env["db"].sessionmaker, env["store"], ev.id) is True
    assert rows(env) == ([], []) and files(env) == []
    assert fe.delete_face_event(env["db"].sessionmaker, env["store"], ev.id) is False


def test_old_events_outside_the_backfill_window_are_not_fetched(env):
    env["settings"].face_backfill_seconds = 3600
    env["frigate"].events = [event(1, start=NOW - 2 * 86400), event(2)]
    ingest(env)
    assert [e.frigate_event_id for e in rows(env)[0]] == [event(2)["id"]]


def test_the_app_starts_the_ingestion_and_retention_loops(settings, frigate):
    from fastapi.testclient import TestClient

    from ai_surveillance.main import create_app

    settings.face_events_enabled = True
    frigate.get("/api/events").respond(200, json=[])
    with TestClient(create_app(settings)) as client:
        names = {t.get_coro().__name__ for t in client.app.state.app.background}
    assert {"face_ingest_loop", "face_retention_loop"} <= names


def test_disabled_ingestion_starts_no_loop(settings, frigate):
    from fastapi.testclient import TestClient

    from ai_surveillance.main import create_app

    settings.face_event_retention_days = 0
    with TestClient(create_app(settings)) as client:
        names = {t.get_coro().__name__ for t in client.app.state.app.background}
    assert "face_ingest_loop" not in names and "face_retention_loop" not in names


def test_started_at_is_stored_in_utc(env):
    env["frigate"].events = [event(1)]
    ingest(env)
    (ev,), _ = rows(env)
    expected = datetime.fromtimestamp(event(1)["start_time"], UTC)
    assert abs(ev.started_at.replace(tzinfo=UTC) - expected) < timedelta(milliseconds=1)
