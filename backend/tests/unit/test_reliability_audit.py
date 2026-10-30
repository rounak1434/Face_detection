"""Security and reliability audit of the face integration: regression tests for concurrency,
memory growth and symlinks. In-process app, real SQLite and files; inference is a respx mock.
"""

import asyncio
import os

import httpx
import pytest
import respx

from ai_surveillance import face_events as fe
from ai_surveillance.db import Database, FaceEvent, Person, ReferenceFace, new_id
from ai_surveillance.frigate_client import FrigateClient
from ai_surveillance.inference_client import InferenceClient
from ai_surveillance.media import MediaStore

from .conftest import INFERENCE
from .test_face_events import FakeFrigate, detect_result, event
from .test_gallery_api import _create, _detect, _enroll, _face, _files

POSIX = os.name == "posix"


# --- a person deleted while an image of them is being enrolled ------------------------------

def test_deactivation_during_enrollment_is_refused(signed_in, settings, frigate):
    person = _create(signed_in)

    def deactivate_meanwhile(request):
        db = Database(settings.database_url)
        with db.sessionmaker() as s:
            s.get(Person, person["id"]).active = False
            s.commit()
        db.engine.dispose()
        return httpx.Response(200, json=_detect(_face()))

    frigate.post(f"{INFERENCE}/v1/embed").mock(side_effect=deactivate_meanwhile)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 409 and resp.json()["error"] == "person_inactive"
    assert _files(settings) == []


def test_enrollment_racing_a_deletion_is_a_clean_404_and_leaves_no_file(signed_in, settings, frigate):
    person = _create(signed_in)

    def delete_meanwhile(request):
        # The person is deleted while the detector is busy with the upload.
        db = Database(settings.database_url)
        with db.sessionmaker() as s:
            s.delete(s.get(Person, person["id"]))
            s.commit()
        db.engine.dispose()
        return httpx.Response(200, json=_detect(_face()))

    frigate.post(f"{INFERENCE}/v1/embed").mock(side_effect=delete_meanwhile)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 404 and "deleted" in resp.json()["detail"]
    assert _files(settings) == []  # the encrypted file written for it was removed again


# --- two uploads racing past the per-person limit -------------------------------------------

def test_the_reference_limit_holds_when_uploads_overlap(signed_in, settings, frigate):
    settings.gallery_max_faces_per_person = 2
    person = _create(signed_in)
    frigate.post(f"{INFERENCE}/v1/embed").respond(200, json=_detect(_face()))
    assert _enroll(signed_in, person["id"]).status_code == 201

    def another_upload_finishes_meanwhile(request):
        db = Database(settings.database_url)
        with db.sessionmaker() as s:  # the other upload's row lands while this one is being analysed
            s.add(ReferenceFace(person_id=person["id"], image_file=f"{new_id()}.enc", media_type="image/jpeg",
                                image_width=640, image_height=480, box_x=1, box_y=1, box_w=100, box_h=100, score=0.9,
                                keypoints="[]", quality="{}", created_by="admin"))
            s.commit()
        db.engine.dispose()
        return httpx.Response(200, json=_detect(_face()))

    frigate.post(f"{INFERENCE}/v1/embed").mock(side_effect=another_upload_finishes_meanwhile)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 409 and resp.json()["error"] == "too_many_faces"
    assert signed_in.get(f"/api/people/{person['id']}").json()["face_count"] == 2
    assert len(_files(settings)) == 1  # no file left behind by the refused upload


# --- retry counters for events that disappear from Frigate ----------------------------------

@pytest.fixture
def env(settings, tmp_path):
    settings.face_frame_source = "snapshot"
    db = Database(settings.database_url)
    db.migrate()
    with respx.mock(assert_all_called=False) as router:
        frigate = FakeFrigate(router)
        detect = router.post(f"{INFERENCE}/v1/detect")
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), db.sessionmaker,
                               MediaStore(tmp_path / "media", "faces"))
        yield frigate, detect, ingestor, db
    db.engine.dispose()


def test_retry_counters_of_vanished_events_are_dropped(env):
    frigate, detect, ingestor, db = env
    vanishing = [event(1, start=fe.utcnow().timestamp() - 300 + i) for i in range(3)]
    detect.respond(503, json={"error": "model_unavailable", "model": "yunet"})
    for e in vanishing:  # each fails once, then Frigate drops it (retention, manual deletion)
        frigate.events = [e]
        asyncio.run(ingestor.ingest_once())
    assert len(ingestor.attempts) == 3
    detect.respond(200, json=detect_result([]))
    frigate.events = [event(9)]
    asyncio.run(ingestor.ingest_once())  # a complete poll
    assert ingestor.attempts == {}
    with db.sessionmaker() as s:
        assert s.query(FaceEvent).count() == 1


def test_a_counter_of_an_event_still_listed_is_kept(env):
    frigate, detect, ingestor, _ = env
    detect.respond(503, json={"error": "model_unavailable", "model": "yunet"})
    frigate.events = [event(1)]
    asyncio.run(ingestor.ingest_once())
    assert ingestor.attempts == {event(1)["id"]: 1}  # the poll stopped on it: kept for the next try


# --- symlinks in the face and gallery stores ------------------------------------------------

@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("subdir", ["faces", "gallery"])
def test_a_planted_symlink_is_never_followed(tmp_path, subdir):
    secret = tmp_path / "outside.txt"
    secret.write_text("not for the browser")
    store = MediaStore(tmp_path / "media", subdir)
    name = f"{'a' * 32}.{'jpg' if subdir == 'faces' else 'enc'}"
    (store.captures_dir / name).symlink_to(secret)
    with pytest.raises(ValueError, match="escapes"):
        store.path_for(name)


@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
def test_face_event_image_behind_a_symlink_is_404(signed_in, settings):
    secret = settings.media_root.parent / "outside.jpg"
    secret.write_bytes(b"\xff\xd8\xff\xe0 secret")
    store = MediaStore(settings.media_root, "faces")
    name = f"{new_id()}.jpg"
    (store.captures_dir / name).symlink_to(secret)
    db = Database(settings.database_url)
    with db.sessionmaker() as s:
        ev = FaceEvent(frigate_event_id="1.0-link", camera="entrance", started_at=fe.utcnow(), ended_at=fe.utcnow(),
                       status="faces_detected", face_count=0, frame_file=name, frame_width=10, frame_height=10)
        s.add(ev)
        s.commit()
        event_id = ev.id
    db.engine.dispose()
    resp = signed_in.get(f"/api/face-events/{event_id}/frame")
    assert resp.status_code == 404 and b"secret" not in resp.content
