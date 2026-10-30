"""The recognition ON/OFF switch: security- and correctness-critical behaviour (ADR-0004).

Complements test_recognition.py (outcomes, API) and test_privacy_review.py (mid-poll changes).
Here the matcher itself is replaced by functions that fail when called, so "off" is proven at
the matching step, not only at the inference call.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai_surveillance import face_events as fe
from ai_surveillance import recognition as rc
from ai_surveillance.db import AppSetting, ReferenceFace
from ai_surveillance.main import create_app

from .conftest import ADMIN_PASSWORD, ADMIN_USER, INFERENCE, JPEG
from .test_face_events import event
from .test_recognition import (SFACE_READY, embed_face, embed_result, gal, ing, similar_to,  # noqa: F401
                               stored, switch, unit)


def _forbid(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not run while recognition is off")
    return fail


@pytest.fixture
def matcher_forbidden(monkeypatch):
    """Every step of identity matching fails the test if it runs."""
    for name in ("match", "match_face", "load_gallery", "embed_missing_references", "current_embedder"):
        monkeypatch.setattr(rc, name, _forbid(name))


def test_off_never_calls_the_matcher_but_keeps_the_event_and_images(ing, gal, matcher_forbidden):  # noqa: F811
    gal("A", vectors=[unit(0)])
    switch(ing["db"], False)
    ing["detect"].respond(200, json={**embed_result(embed_face(None)), "embedder": None})
    ing["frigate"].events = [event(1)]
    stats = asyncio.run(ing["ingestor"].ingest_once())
    assert stats.stored == 1 and stats.stopped is None
    (ev,), (det,) = stored(ing)
    assert ev.status == "faces_detected" and ev.recognition_state == "off" and ev.recognizer is None
    assert ev.frame_file and (ing["ingestor"].store.path_for(ev.frame_file)).read_bytes() == JPEG
    assert det.outcome == "NOT_MATCHED" and det.person_id is None and det.similarity is None
    assert ing["embed"].call_count == 0 and ing["detect"].call_count == 1


def test_control_the_forbidden_matcher_does_trip_when_recognition_is_on(ing, gal, matcher_forbidden):  # noqa: F811
    """Without this, the test above could pass because the spy never sat on the real path."""
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["frigate"].events = [event(1)]
    with pytest.raises(AssertionError, match="must not run while recognition is off"):
        asyncio.run(ing["ingestor"].ingest_once())
    assert stored(ing)[0] == []


def test_no_setting_row_means_off_and_the_matcher_is_never_called(ing, gal, matcher_forbidden):  # noqa: F811
    gal("A", vectors=[unit(0)])  # a gallery exists, but nobody switched recognition on
    ing["detect"].respond(200, json={**embed_result(embed_face(None)), "embedder": None})
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    assert stored(ing)[1][0].outcome == "NOT_MATCHED"


@pytest.mark.parametrize("value", ["true", '"yes"', "1", "not json", "null"])
def test_a_damaged_setting_value_fails_closed(gal, value):  # noqa: F811
    with gal.db.sessionmaker() as s:
        s.add(AppSetting(key="recognition_enabled", value=value, updated_by="x"))
        s.commit()
        expected = value == "true"  # only the JSON value true switches it on
        assert rc.is_enabled(s) is expected


def test_switching_off_keeps_the_gallery_and_embeddings(signed_in, frigate, gal):  # noqa: F811
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    pid = gal("A", vectors=[unit(0), unit(1)])
    with gal.db.sessionmaker() as s:
        before = {f.id: bytes(f.embedding_enc) for f in s.query(ReferenceFace).filter_by(person_id=pid)}
    on = signed_in.put("/api/recognition", json={"enabled": True, "password": ADMIN_PASSWORD})
    off = signed_in.put("/api/recognition", json={"enabled": False, "password": ADMIN_PASSWORD})
    assert on.status_code == off.status_code == 200 and off.json()["enabled"] is False
    with gal.db.sessionmaker() as s:
        after = {f.id: bytes(f.embedding_enc) for f in s.query(ReferenceFace).filter_by(person_id=pid)}
    assert after == before  # byte-for-byte: nothing re-encrypted, nothing deleted
    assert signed_in.get(f"/api/people/{pid}").json()["face_count"] == 2
    assert off.json()["gallery"]["reference_faces"] == 2


def test_the_setting_survives_a_restart(signed_in, settings, frigate, gal):  # noqa: F811
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    gal("A", vectors=[unit(0)])
    assert signed_in.put("/api/recognition", json={"enabled": True, "password": ADMIN_PASSWORD}).json()["enabled"]
    for expected in (True, False):
        with TestClient(create_app(settings)) as restarted:  # a fresh app on the same database
            restarted.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
            assert restarted.get("/api/recognition").json()["enabled"] is expected
            if expected:
                restarted.put("/api/recognition", json={"enabled": False, "password": ADMIN_PASSWORD})


def test_the_default_after_a_fresh_install_is_off(signed_in, frigate):
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    body = signed_in.get("/api/recognition").json()
    assert body["enabled"] is False and body["updated_by"] is None


def test_switching_on_does_not_identify_past_events(ing, gal):  # noqa: F811
    pid = gal("A", vectors=[unit(0)])
    ing["detect"].respond(200, json={**embed_result(embed_face(None)), "embedder": None})
    ing["embed"].respond(200, json=embed_result(embed_face(similar_to(0, 0.9))))
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())  # off
    switch(ing["db"], True)
    ing["frigate"].events = [event(1), event(2)]  # Frigate lists the old event again
    asyncio.run(ing["ingestor"].ingest_once())
    events, dets = stored(ing)
    assert [(e.recognition_state, d.outcome, d.person_id) for e, d in zip(events, dets, strict=True)] == [
        ("off", "NOT_MATCHED", None), ("done", "AUTHORIZED", pid)]
    assert ing["embed"].call_count == 1  # only the new event


def test_recognition_failure_while_on_is_recorded_not_hidden(ing, gal):  # noqa: F811
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["embed"].respond(200, json=embed_result(embed_face(None)))  # a face without an embedding
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    (ev,), (det,) = stored(ing)
    assert ev.recognition_state == "done" and det.outcome == "SYSTEM_ERROR"
    assert rc.outcome_reason(det.outcome, det.similarity, det.accept_threshold, ev.recognition_state) == \
        "recognition_error"


def test_states_record_off_unavailable_and_attempted(ing, gal):  # noqa: F811
    gal("A", vectors=[unit(0)])
    ing["detect"].respond(200, json={**embed_result(embed_face(None)), "embedder": None})
    ing["embed"].respond(200, json=embed_result(embed_face(similar_to(0, 0.9))))
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())  # off
    switch(ing["db"], True)
    ing["models"].respond(200, json=[SFACE_READY[0], {**SFACE_READY[1], "status": "missing"}])
    ing["frigate"].events = [event(1), event(2)]
    asyncio.run(fe.Ingestor(ing["ingestor"].settings, ing["ingestor"].frigate, ing["ingestor"].inference,
                            ing["ingestor"].sessions, ing["ingestor"].store, cipher=ing["ingestor"].cipher,
                            gallery_store=ing["ingestor"].gallery_store).ingest_once())  # on, embedder missing
    ing["models"].respond(200, json=SFACE_READY)
    ing["frigate"].events = [event(1), event(2), event(3)]
    asyncio.run(ing["ingestor"].ingest_once())  # on, embedder ready
    events, dets = stored(ing)
    assert [e.recognition_state for e in events] == ["off", "unavailable", "done"]
    reasons = [rc.outcome_reason(d.outcome, d.similarity, d.accept_threshold, e.recognition_state)
               for e, d in zip(events, dets, strict=True)]
    assert reasons == ["recognition_off", "recognition_unavailable", "match"]


def test_only_the_server_decides(signed_in, frigate, gal):  # noqa: F811
    """No client-side flag reaches matching: the event API has no way to request recognition."""
    paths = signed_in.app.openapi()["paths"]
    assert set(paths["/api/recognition"]) == {"get", "put"}
    params = {p["name"] for op in paths["/api/face-events"].values() for p in op.get("parameters", [])}
    assert not params & {"recognize", "recognition", "match", "enabled"}
    resp = signed_in.put("/api/recognition", json={"enabled": "true", "password": ADMIN_PASSWORD})
    assert resp.status_code == 422  # a string is not a boolean: no truthy coercion


def test_unauthorized_requests_change_nothing(client, frigate, gal):  # noqa: F811
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    gal("A", vectors=[unit(0)])
    assert client.put("/api/recognition", json={"enabled": True, "password": ADMIN_PASSWORD}).status_code == 401
    assert client.get("/api/recognition").status_code == 401
    with gal.db.sessionmaker() as s:
        assert not rc.is_enabled(s) and s.get(AppSetting, "recognition_enabled") is None
