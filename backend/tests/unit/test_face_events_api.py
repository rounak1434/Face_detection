"""Face event history API (ADR-0004, Phase 4). In-process app, real SQLite, real files."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from ai_surveillance.db import Database, FaceDetection, FaceEvent, new_id
from ai_surveillance.media import MediaStore

FRAME = b"\xff\xd8\xff\xe0frame\xff\xd9"
CROP = b"\xff\xd8\xff\xe0crop\xff\xd9"
T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def seed(signed_in, settings):
    db = Database(settings.database_url)
    store = MediaStore(settings.media_root, "faces")

    def add(minutes: float, camera="entrance", status="faces_detected", faces=1, frame=True, event_id=None):
        frame_file = None
        if frame and faces:
            frame_file = f"{new_id()}.jpg"
            store.write_atomic(frame_file, FRAME)
        with db.sessionmaker() as s:
            ev = FaceEvent(id=event_id or new_id(), frigate_event_id=f"{1761739200 + minutes * 60:.6f}-x{new_id()[:6]}",
                           camera=camera, started_at=T0 + timedelta(minutes=minutes),
                           ended_at=T0 + timedelta(minutes=minutes, seconds=5), status=status, face_count=faces,
                           frame_source="recording" if frame_file else None, frame_file=frame_file,
                           frame_width=1920 if frame_file else None, frame_height=1080 if frame_file else None,
                           detector="yunet 2023mar")
            s.add(ev)
            s.flush()
            for i in range(faces):
                crop_file = f"{new_id()}.jpg"
                store.write_atomic(crop_file, CROP)
                s.add(FaceDetection(event_id=ev.id, position=i, box_x=10 * i, box_y=20, box_w=80, box_h=100,
                                    score=0.9 - i / 10, keypoints=json.dumps([[1, 2]] * 5),
                                    quality=json.dumps({"face_px": 80, "small_face": False}), crop_file=crop_file))
            s.commit()
            return ev.id

    add.db, add.store = db, store
    yield add
    db.engine.dispose()


def _files(seed):
    return sorted(p.name for p in seed.store.captures_dir.iterdir())


def test_every_route_requires_sign_in(client):
    eid = "a" * 32
    for method, path in [("get", "/api/face-events"), ("get", f"/api/face-events/{eid}"),
                         ("get", f"/api/face-events/{eid}/frame"),
                         ("get", f"/api/face-events/{eid}/detections/{eid}/crop"),
                         ("delete", f"/api/face-events/{eid}")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_list_is_newest_first_with_detections_and_no_file_names(signed_in, seed, settings):
    older, newer = seed(1), seed(2, faces=2)
    body = signed_in.get("/api/face-events").json()
    assert [e["id"] for e in body["items"]] == [newer, older] and body["next_cursor"] is None
    item = body["items"][0]
    assert item["camera"] == "entrance" and item["status"] == "faces_detected" and item["face_count"] == 2
    assert item["started_at"] == (T0 + timedelta(minutes=2)).isoformat()
    assert item["frame"] == {"width": 1920, "height": 1080} and item["frame_url"] == f"/api/face-events/{newer}/frame"
    assert [d["position"] for d in item["detections"]] == [0, 1]
    det = item["detections"][0]
    assert det["box"] == {"x": 0, "y": 20, "w": 80, "h": 100} and det["score"] == 0.9
    assert det["crop_url"] == f"/api/face-events/{newer}/detections/{det['id']}/crop"
    text = json.dumps(body)
    for name in _files(seed):
        assert name not in text  # server-side file names never leave the server
    assert str(settings.media_root) not in text and "faces" not in text.replace("face", "")


def test_pages_are_stable_and_complete(signed_in, seed):
    ids = [seed(m) for m in (1, 2, 3, 4, 5)]
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = signed_in.get("/api/face-events", params=params).json()
        seen += [e["id"] for e in body["items"]]
        pages += 1
        if pages == 1:
            seed(10)  # a new event arriving while paging must not shift the next pages
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(ids)) and pages == 3


def test_events_with_the_same_start_time_are_neither_skipped_nor_repeated(signed_in, seed):
    ids = sorted(seed(1, event_id=new_id()) for _ in range(3))
    first = signed_in.get("/api/face-events", params={"limit": 2}).json()
    second = signed_in.get("/api/face-events", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    got = [e["id"] for e in first["items"] + second["items"]]
    assert sorted(got) == ids and len(set(got)) == 3


def test_filters(signed_in, seed):
    a = seed(1, camera="entrance")
    b = seed(2, camera="gate")
    c = seed(3, camera="gate", status="no_faces_detected", faces=0)
    d = seed(4, camera="gate", status="gave_up", faces=0, frame=False)

    def ids(**params):
        return [e["id"] for e in signed_in.get("/api/face-events", params=params).json()["items"]]

    assert ids(camera="gate") == [d, c, b]
    assert ids(status="no_faces_detected") == [c]
    assert ids(has_faces="true") == [b, a] and ids(has_faces="false") == [d, c]
    assert ids(since=(T0 + timedelta(minutes=2)).isoformat(), until=(T0 + timedelta(minutes=4)).isoformat()) == [c, b]
    # An offset is converted: 17:32+05:30 is 12:02 UTC.
    assert ids(since="2026-10-29T17:32:00+05:30") == [d, c, b]
    assert ids(since="2026-10-29T12:02:00") == [d, c, b]  # no offset: UTC


@pytest.mark.parametrize("params", [{"status": "AUTHORIZED"}, {"cursor": "not-a-cursor"}, {"cursor": "W10"},
                                    {"limit": 0}, {"limit": 201}, {"since": "yesterday"}])
def test_invalid_queries_are_refused(signed_in, seed, params):
    seed(1)
    assert signed_in.get("/api/face-events", params=params).status_code == 422


def test_detail_and_unknown_ids(signed_in, seed):
    eid = seed(1)
    body = signed_in.get(f"/api/face-events/{eid}").json()
    assert body["id"] == eid and len(body["detections"]) == 1
    for bad in ["b" * 32, "../etc", "A" * 32]:
        assert signed_in.get(f"/api/face-events/{bad}").status_code == 404


def test_frame_and_crop_images(signed_in, seed):
    eid = seed(1)
    det = signed_in.get(f"/api/face-events/{eid}").json()["detections"][0]
    frame = signed_in.get(f"/api/face-events/{eid}/frame")
    assert frame.status_code == 200 and frame.content == FRAME
    assert frame.headers["content-type"] == "image/jpeg" and frame.headers["cache-control"] == "private, no-store"
    crop = signed_in.get(det["crop_url"])
    assert crop.status_code == 200 and crop.content == CROP


def test_a_crop_is_only_reachable_through_its_own_event(signed_in, seed):
    first, second = seed(1), seed(2)
    det = signed_in.get(f"/api/face-events/{first}").json()["detections"][0]
    assert signed_in.get(f"/api/face-events/{second}/detections/{det['id']}/crop").status_code == 404


def test_missing_images_give_404(signed_in, seed):
    no_frame = seed(1, status="no_faces_detected", faces=0)
    assert signed_in.get(f"/api/face-events/{no_frame}").json()["frame_url"] is None
    assert signed_in.get(f"/api/face-events/{no_frame}/frame").status_code == 404
    eid = seed(2)
    for name in _files(seed):
        (seed.store.captures_dir / name).unlink()
    resp = signed_in.get(f"/api/face-events/{eid}/frame")
    assert resp.status_code == 404 and resp.json()["detail"] == "Image missing on disk"


def test_delete_removes_rows_and_images(signed_in, seed):
    keep, gone = seed(1), seed(2, faces=2)
    keep_files = set(_files(seed))
    assert signed_in.delete(f"/api/face-events/{gone}").status_code == 204
    assert [e["id"] for e in signed_in.get("/api/face-events").json()["items"]] == [keep]
    with seed.db.sessionmaker() as s:
        assert s.query(FaceDetection).filter_by(event_id=gone).count() == 0
    assert len(_files(seed)) == 2 and set(_files(seed)) < keep_files  # only the kept event's frame and crop
    assert signed_in.delete(f"/api/face-events/{gone}").status_code == 404


def test_delete_keeps_the_event_when_an_image_cannot_be_removed(signed_in, seed, monkeypatch, settings):
    eid = seed(1)

    def refuse(self, name):
        raise PermissionError(13, "Permission denied", str(self.captures_dir / name))

    monkeypatch.setattr(MediaStore, "delete", refuse)
    resp = signed_in.delete(f"/api/face-events/{eid}")
    assert resp.status_code == 500
    assert "Permission denied" in resp.json()["detail"] and str(settings.media_root) not in resp.text
    assert signed_in.get(f"/api/face-events/{eid}").status_code == 200
