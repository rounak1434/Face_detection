"""Security and privacy review (ADR-0004, Phase 8): regression tests for the findings and sweeps
over every route. In-process app, real SQLite and files; the inference service is a respx mock.
"""

import asyncio
import base64
import re
import sqlite3
import struct
from pathlib import Path

import httpx
import pytest
import respx

from ai_surveillance import face_events as fe
from ai_surveillance.db import AuditEntry, Database, Person
from ai_surveillance.frigate_client import FrigateClient
from ai_surveillance.inference_client import InferenceClient
from ai_surveillance.media import MediaStore

from .conftest import ADMIN_PASSWORD, INFERENCE
from .test_face_events import FakeFrigate, event
from .test_recognition import SFACE_READY, embed_face, embed_result, gal, similar_to, stored, switch, unit  # noqa: F401

MARKER = "consent-marker-7Q2ZK"


# --- finding: deleted rows stayed readable in the database file ------------------------------

def _raw_database(db_path: Path) -> bytes:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    wal = db_path.with_name(db_path.name + "-wal")
    return db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


def test_deleted_person_is_not_left_in_free_pages(signed_in, settings):
    """SQLite keeps deleted rows in free pages until reused; secure_delete overwrites them."""
    person = signed_in.post("/api/people", json={"display_name": "Erasable Person", "consent_date": "2026-10-01",
                                                 "consent_note": MARKER}).json()
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    assert MARKER.encode() in _raw_database(db_path)
    resp = signed_in.request("DELETE", f"/api/people/{person['id']}", json={"password": ADMIN_PASSWORD})
    assert resp.status_code == 204
    raw = _raw_database(db_path)
    assert MARKER.encode() not in raw and b"Erasable Person" not in raw


def test_secure_delete_is_on(settings):
    db = Database(settings.database_url)
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA secure_delete").scalar() == 1
    db.engine.dispose()


# --- finding: deleting a face event was not audited ------------------------------------------

def test_face_event_deletion_is_audited(signed_in, settings, gal):  # noqa: F811
    switch(gal.db, False)
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        fr = FakeFrigate(router)
        router.post(f"{INFERENCE}/v1/detect").respond(200, json={**embed_result(embed_face(None)), "embedder": None})
        fr.events = [event(1)]
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), gal.db.sessionmaker,
                               MediaStore(settings.media_root, "faces"))
        asyncio.run(ingestor.ingest_once())
    event_id = signed_in.get("/api/face-events").json()["items"][0]["id"]
    assert signed_in.delete(f"/api/face-events/{event_id}").status_code == 204
    with gal.db.sessionmaker() as s:
        entry = s.query(AuditEntry).filter_by(action="face_event_deleted").one()
    assert (entry.username, entry.target_type, entry.target_id) == ("admin", "face_event", event_id)


# --- finding: switch and gallery changes waited for the next poll ----------------------------

@pytest.fixture
def poll(settings, gal, tmp_path):  # noqa: F811
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        fr = FakeFrigate(router)
        router.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
        detect = router.post(f"{INFERENCE}/v1/detect").respond(
            200, json={**embed_result(embed_face(None)), "embedder": None})
        embed = router.post(f"{INFERENCE}/v1/embed")
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), gal.db.sessionmaker,
                               MediaStore(tmp_path / "media", "faces"), cipher=gal.cipher, gallery_store=gal.store)
        yield {"frigate": fr, "detect": detect, "embed": embed, "ingestor": ingestor, "db": gal.db}


def _after_first_embed(action):
    """/v1/embed answers a matching face; after the first call, `action` changes the database."""
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            action()
        return httpx.Response(200, json=embed_result(embed_face(similar_to(0, 0.9))))
    return respond


def test_switching_off_mid_poll_stops_matching_at_the_next_event(poll, gal):  # noqa: F811
    gal("A", vectors=[unit(0)])
    switch(poll["db"], True)
    poll["embed"].mock(side_effect=_after_first_embed(lambda: switch(poll["db"], False)))
    poll["frigate"].events = [event(1), event(2), event(3)]
    asyncio.run(poll["ingestor"].ingest_once())
    events, dets = stored(poll)
    assert [e.recognition_state for e in events] == ["done", "off", "off"]
    assert [d.outcome for d in dets] == ["AUTHORIZED", "NOT_MATCHED", "NOT_MATCHED"]
    assert poll["embed"].call_count == 1 and poll["detect"].call_count == 2


def test_deactivating_a_person_mid_poll_stops_their_matches(poll, gal):  # noqa: F811
    pid = gal("A", vectors=[unit(0)])
    switch(poll["db"], True)

    def deactivate():
        with poll["db"].sessionmaker() as s:
            s.get(Person, pid).active = False
            s.commit()

    poll["embed"].mock(side_effect=_after_first_embed(deactivate))
    poll["frigate"].events = [event(1), event(2)]
    asyncio.run(poll["ingestor"].ingest_once())
    _, dets = stored(poll)
    assert [(d.outcome, d.person_id) for d in dets] == [("AUTHORIZED", pid), ("UNRECOGNIZED", None)]


def test_switching_on_mid_poll_starts_matching(poll, gal):  # noqa: F811
    gal("A", vectors=[unit(0)])
    poll["detect"].mock(side_effect=lambda req: (switch(poll["db"], True), httpx.Response(
        200, json={**embed_result(embed_face(None)), "embedder": None}))[1])
    poll["embed"].respond(200, json=embed_result(embed_face(similar_to(0, 0.9))))
    poll["frigate"].events = [event(1), event(2)]
    asyncio.run(poll["ingestor"].ingest_once())
    events, dets = stored(poll)
    assert [e.recognition_state for e in events] == ["off", "done"]
    assert [d.outcome for d in dets] == ["NOT_MATCHED", "AUTHORIZED"]


# --- sweeps over every route -----------------------------------------------------------------

PUBLIC = {("GET", "/api/health"), ("POST", "/api/auth/login"), ("GET", "/api/auth/me"),
          ("POST", "/api/auth/logout")}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "a" * 32, path)


def test_every_route_except_the_public_ones_needs_sign_in(client):
    paths = client.app.openapi()["paths"]
    checked = 0
    for path, ops in paths.items():
        for method in ops:
            if (method.upper(), path) in PUBLIC:
                continue
            resp = client.request(method.upper(), _concrete(path))
            assert resp.status_code == 401, (method, path, resp.status_code)
            checked += 1
    assert checked >= 30  # the sweep really covered the API
    assert client.get("/api/auth/me").status_code == 401


def test_responses_never_carry_files_paths_or_embeddings(signed_in, settings, frigate, gal):  # noqa: F811
    """Enroll, match an event, then read every list and detail route and scan the answers."""
    vector = similar_to(0, 0.9)
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    face = {**embed_face(vector, box={"x": 200, "y": 120, "w": 160, "h": 200})}
    frigate.post(f"{INFERENCE}/v1/embed").respond(200, json={**embed_result(face), "image": {
        "width": 640, "height": 480, "format": "image/jpeg"}})
    person = signed_in.post("/api/people", json={"display_name": "Sweep Person", "consent_date": "2026-10-01",
                                                 "consent_note": "form"}).json()
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 200
    assert signed_in.post(f"/api/people/{person['id']}/faces", files={"file": ("a.jpg", jpeg, "image/jpeg")}).status_code == 201
    assert signed_in.put("/api/recognition", json={"enabled": True, "password": ADMIN_PASSWORD}).status_code == 200
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        fr = FakeFrigate(router)
        router.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
        router.post(f"{INFERENCE}/v1/embed").respond(200, json=embed_result(embed_face(vector)))
        fr.events = [event(1)]
        state = signed_in.app.state.app
        asyncio.run(fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), state.db.sessionmaker,
                                state.face_store, cipher=state.cipher, gallery_store=state.gallery_store).ingest_once())
    event_id = signed_in.get("/api/face-events").json()["items"][0]["id"]
    assert signed_in.get("/api/face-events").json()["items"][0]["detections"][0]["recognition"]["outcome"] == "AUTHORIZED"

    files = [p.name for p in Path(settings.media_root).rglob("*") if p.is_file()]
    assert files  # there is something to leak
    forbidden = [str(Path(settings.media_root).resolve()), "media/", "\\media", ".enc", "embedding_enc",
                 base64.b64encode(struct.pack("<128f", *vector)).decode()[:40], *files]
    for path in ["/api/face-events", f"/api/face-events/{event_id}", "/api/people", f"/api/people/{person['id']}",
                 "/api/recognition", "/api/audit-log"]:
        text = signed_in.get(path).text
        for needle in forbidden:
            assert needle not in text, (path, needle)
        assert '"embedding"' not in text and '"embedding_enc"' not in text, path
