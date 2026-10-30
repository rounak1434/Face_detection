"""Recognition (ADR-0004, Phase 6): matcher, reference embeddings, the switch, and matching in ingestion.

The inference service is a respx mock answering in /v1/embed's shape with chosen vectors, so the
similarities are exact and every outcome can be produced on purpose. Real SFace on real photos is
covered by test_real_sface_* at the end (skipped unless the inference package and both models are
installed) and by inference/tests/test_model_sface.py.
"""

import asyncio
import base64
import json
import math
import struct
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from ai_surveillance import face_events as fe
from ai_surveillance import recognition as rc
from ai_surveillance.config import Settings
from ai_surveillance.credentials import CredentialCipher, load_key
from ai_surveillance.db import AuditEntry, Database, FaceDetection, FaceEvent, Person, ReferenceFace, new_id
from ai_surveillance.frigate_client import FrigateClient
from ai_surveillance.gallery import image_context
from ai_surveillance.inference_client import InferenceClient, InferenceUnavailable
from ai_surveillance.media import MediaStore

from .conftest import ADMIN_PASSWORD, ADMIN_USER, FRIGATE, INFERENCE, JPEG
from .test_face_events import FakeFrigate, event

SFACE_READY = [{"name": "yunet", "version": "2023mar", "status": "ready"},
               {"name": "sface", "version": "2021dec", "status": "ready"}]
SFACE_MISSING = [SFACE_READY[0], {**SFACE_READY[1], "status": "missing"}]
EMBEDDER = {"name": "sface", "version": "2021dec", "dimensions": 128}
REF_BOX = {"x": 200, "y": 120, "w": 160, "h": 200}


def unit(i: int) -> list[float]:
    v = [0.0] * 128
    v[i] = 1.0
    return v


def similar_to(i: int, similarity: float, j: int = 127) -> list[float]:
    """A unit vector whose cosine similarity with unit(i) is `similarity`."""
    v = [0.0] * 128
    v[i], v[j] = similarity, math.sqrt(1 - similarity ** 2)
    return v


def b64(v: list[float]) -> str:
    return base64.b64encode(struct.pack("<128f", *v)).decode()


def embed_face(v, box=None, score=0.95):
    box = box or {"x": 10, "y": 20, "w": 80, "h": 100}
    return {"index": 0, "box": box, "score": score, "keypoints5": [[30, 50], [60, 50], [45, 70], [35, 90], [55, 90]],
            "quality": {"face_px": min(box["w"], box["h"])}, "crop": None,
            **({"embedding": b64(v)} if v is not None else {})}


def embed_result(*faces):
    return {"status": "x", "image": {"width": 1280, "height": 720, "format": "image/jpeg"}, "faces": list(faces),
            "face_count": len(faces), "model": {"name": "yunet", "version": "2023mar"}, "embedder": EMBEDDER}


S = Settings(_env_file=None)


# --- matcher ---------------------------------------------------------------------------------

def gallery(*entries) -> rc.Gallery:
    return rc.Gallery("sface 2021dec", [(p, tuple(v)) for p, v in entries])


@pytest.mark.parametrize("sim, outcome, person", [
    (0.80, rc.AUTHORIZED, "a"), (0.45, rc.AUTHORIZED, "a"), (0.44, rc.INCONCLUSIVE, None),
    (0.31, rc.INCONCLUSIVE, None), (0.30, rc.UNRECOGNIZED, None), (0.05, rc.UNRECOGNIZED, None),
])
def test_thresholds(sim, outcome, person):
    m = rc.match(tuple(similar_to(0, sim)), gallery(("a", unit(0)), ("b", unit(1))), S)
    assert (m.outcome, m.person_id) == (outcome, person) and m.similarity == pytest.approx(sim, abs=1e-4)


def test_best_reference_per_person_counts():
    m = rc.match(tuple(similar_to(0, 0.9)), gallery(("a", unit(5)), ("a", unit(0)), ("b", unit(1))), S)
    assert (m.outcome, m.person_id) == (rc.AUTHORIZED, "a")


def test_two_people_above_accept_is_inconclusive():
    v = [0.0] * 128
    v[0] = v[1] = math.sqrt(0.5)  # 0.707 to both
    m = rc.match(tuple(v), gallery(("a", unit(0)), ("b", unit(1))), S)
    assert m.outcome == rc.INCONCLUSIVE and m.person_id is None


def test_empty_gallery_small_face_and_missing_embedding():
    assert rc.match(tuple(unit(0)), gallery(), S).outcome == rc.UNRECOGNIZED
    small = rc.match_face({"x": 0, "y": 0, "w": 39, "h": 80}, tuple(unit(0)), gallery(("a", unit(0))), S)
    assert small.outcome == rc.INCONCLUSIVE and small.similarity is None
    assert rc.match_face({"x": 0, "y": 0, "w": 80, "h": 80}, None, gallery(("a", unit(0))), S).outcome == rc.SYSTEM_ERROR


def test_embedding_decoding_rejects_bad_values():
    assert rc.decode_embedding(b64([2.0] + [0.0] * 127))[0] == pytest.approx(1.0)  # normalised
    for bad in (None, 5, "not base64!", base64.b64encode(b"\x00" * 100).decode(), b64([0.0] * 128),
                b64([float("nan")] + [0.0] * 127)):
        assert rc.decode_embedding(bad) is None


def test_thresholds_must_be_ordered():
    with pytest.raises(ValidationError):
        Settings(recognition_accept_threshold=0.3, recognition_reject_threshold=0.3, _env_file=None)


# --- shared database fixtures ---------------------------------------------------------------

@pytest.fixture
def gal(settings):
    db = Database(settings.database_url)
    db.migrate()
    cipher = CredentialCipher(load_key(settings))
    store = MediaStore(settings.media_root, "gallery")

    def person(name="P", active=True, vectors=(), embedder="sface 2021dec", image=JPEG):
        with db.sessionmaker() as s:
            p = Person(display_name=name, consent_date=date(2026, 10, 1), consent_note="form", created_by="admin",
                       active=active)
            s.add(p)
            s.flush()
            for v in vectors or [None]:
                fid, file_name = new_id(), f"{new_id()}.enc"
                store.write_atomic(file_name, cipher.encrypt_bytes(image, image_context(fid)))
                f = ReferenceFace(id=fid, person_id=p.id, image_file=file_name, media_type="image/jpeg",
                                  image_width=640, image_height=480, score=0.95, keypoints="[]", quality="{}",
                                  created_by="admin", **{f"box_{k}": REF_BOX[k] for k in "xywh"})
                if v is not None:
                    rc.store_reference_embedding(s, cipher, f, tuple(v), embedder)
                s.add(f)
            s.commit()
            return p.id

    person.db, person.cipher, person.store = db, cipher, store
    yield person
    db.engine.dispose()


def test_gallery_loads_active_people_of_the_current_embedder_only(gal):
    a = gal("A", vectors=[unit(0), unit(1)])
    gal("Inactive", active=False, vectors=[unit(2)])
    gal("Old model", vectors=[unit(3)], embedder="sface 2020")
    gal("Not embedded")
    g = rc.load_gallery(gal.db.sessionmaker, gal.cipher, "sface 2021dec")
    assert sorted(p for p, _ in g.entries) == [a, a] and g.people == 1 and g.unreadable == 0


def test_tampered_embedding_is_skipped_and_counted(gal):
    a = gal("A", vectors=[unit(0)])
    b = gal("B", vectors=[unit(1)])
    with gal.db.sessionmaker() as s:
        fa = s.query(ReferenceFace).filter_by(person_id=a).one()
        fb = s.query(ReferenceFace).filter_by(person_id=b).one()
        fb.embedding_enc = fa.embedding_enc  # bound to A's face id: does not decrypt as B's
        s.commit()
    g = rc.load_gallery(gal.db.sessionmaker, gal.cipher, "sface 2021dec")
    assert [p for p, _ in g.entries] == [a] and g.unreadable == 1


def _run(coro):
    return asyncio.run(coro)


def test_missing_reference_embeddings_are_computed_from_the_encrypted_image(gal, settings):
    pid = gal("A")  # enrolled without SFace
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{INFERENCE}/v1/embed").respond(
            200, json=embed_result(embed_face(unit(7), box={"x": 205, "y": 118, "w": 150, "h": 205}),
                                   embed_face(unit(9), box={"x": 0, "y": 0, "w": 40, "h": 40})))
        done = _run(rc.embed_missing_references(gal.db.sessionmaker, InferenceClient(settings), gal.cipher, gal.store,
                                                settings, "sface 2021dec"))
    assert done == (1, 0)
    assert route.calls.last.request.content == JPEG  # the decrypted reference image
    g = rc.load_gallery(gal.db.sessionmaker, gal.cipher, "sface 2021dec")
    assert g.entries == [(pid, tuple(unit(7)))]  # the face overlapping the enrolled box, not the other one


def test_reference_whose_face_is_not_found_again_is_skipped_not_retried(gal, settings):
    gal("A")
    skip: set[str] = set()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{INFERENCE}/v1/embed").respond(
            200, json=embed_result(embed_face(unit(1), box={"x": 0, "y": 0, "w": 50, "h": 50})))
        for _ in range(2):
            result = _run(rc.embed_missing_references(gal.db.sessionmaker, InferenceClient(settings), gal.cipher,
                                                      gal.store, settings, "sface 2021dec", skip))
    assert result == (0, 0) and len(skip) == 1 and route.call_count == 1


def test_embedder_outage_while_embedding_references_raises(gal, settings):
    gal("A")
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{INFERENCE}/v1/embed").respond(503, json={"error": "model_unavailable", "model": "sface"})
        with pytest.raises(InferenceUnavailable):
            _run(rc.embed_missing_references(gal.db.sessionmaker, InferenceClient(settings), gal.cipher, gal.store,
                                             settings, "sface 2021dec"))


# --- the switch API --------------------------------------------------------------------------

def _put(client, enabled, password=ADMIN_PASSWORD):
    return client.put("/api/recognition", json={"enabled": enabled, "password": password})


def test_switch_requires_sign_in_and_password(client, signed_in, frigate):
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    signed_in.cookies.clear()
    assert client.get("/api/recognition").status_code == 401 and _put(client, True).status_code == 401
    signed_in.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    body = signed_in.get("/api/recognition").json()
    assert body["enabled"] is False and body["embedder"] == {"ready": True, "model": "sface 2021dec",
                                                             "status": "ready"}
    assert body["thresholds"] == {"accept": 0.45, "reject": 0.30, "min_face_px": 40}
    assert _put(signed_in, True, "wrong-password-123").status_code == 403
    assert signed_in.put("/api/recognition", json={"enabled": True}).status_code == 422
    assert signed_in.get("/api/recognition").json()["enabled"] is False


def test_switch_on_is_refused_without_embedder_or_gallery(signed_in, frigate, gal):
    models = frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_MISSING)
    resp = _put(signed_in, True)
    assert resp.status_code == 409 and resp.json()["error"] == "embedder_unavailable" and "missing" in resp.text
    models.respond(200, json=SFACE_READY)
    resp = _put(signed_in, True)
    assert resp.status_code == 409 and resp.json()["error"] == "gallery_empty"
    gal("Inactive", active=False, vectors=[unit(0)])
    assert _put(signed_in, True).json()["error"] == "gallery_empty"
    assert signed_in.get("/api/recognition").json()["enabled"] is False


def test_switch_on_embeds_pending_references_and_is_audited(signed_in, frigate, gal):
    frigate.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
    embed = frigate.post(f"{INFERENCE}/v1/embed").respond(200, json=embed_result(embed_face(unit(4), box=REF_BOX)))
    gal("A")
    resp = _put(signed_in, True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True and body["updated_by"] == ADMIN_USER and embed.call_count == 1
    assert body["gallery"] == {"counts_known": True, "total_reference_faces": 1, "people": 1, "reference_faces": 1,
                               "pending": 0, "unreadable": 0}
    assert _put(signed_in, False).json()["enabled"] is False
    with gal.db.sessionmaker() as s:
        actions = [(e.action, e.target_id) for e in s.query(AuditEntry).order_by(AuditEntry.at)]
    assert ("recognition_enabled", "recognition") in actions and ("recognition_disabled", "recognition") in actions


# --- matching in ingestion -------------------------------------------------------------------

@pytest.fixture
def ing(settings, gal, tmp_path):
    settings.face_frame_source = "snapshot"
    store = MediaStore(tmp_path / "media", "faces")
    with respx.mock(assert_all_called=False) as router:
        frigate = FakeFrigate(router)
        models = router.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
        detect = router.post(f"{INFERENCE}/v1/detect").respond(
            200, json={**embed_result(embed_face(None)), "embedder": None})
        embed = router.post(f"{INFERENCE}/v1/embed")
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), gal.db.sessionmaker, store,
                               cipher=gal.cipher, gallery_store=gal.store)
        yield {"frigate": frigate, "models": models, "detect": detect, "embed": embed, "ingestor": ingestor,
               "db": gal.db}


def switch(db, on: bool):
    with db.sessionmaker() as s:
        rc.set_enabled(s, on, "admin")
        s.commit()


def stored(ing):
    with ing["db"].sessionmaker() as s:
        return s.query(FaceEvent).order_by(FaceEvent.started_at).all(), s.query(FaceDetection).order_by(
            FaceDetection.position).all()


def test_switch_off_never_embeds(ing, gal):
    gal("A", vectors=[unit(0)])
    ing["frigate"].events = [event(1), event(2)]
    asyncio.run(ing["ingestor"].ingest_once())
    assert ing["detect"].call_count == 2 and ing["embed"].call_count == 0 and not ing["models"].called
    (e1, e2), dets = stored(ing)
    assert e1.recognition_state == e2.recognition_state == "off" and e1.recognizer is None
    assert rc.outcome_reason(dets[0].outcome, dets[0].similarity, dets[0].accept_threshold,
                             e1.recognition_state) == "recognition_off"
    assert {d.outcome for d in dets} == {"NOT_MATCHED"} and {d.person_id for d in dets} == {None}


def test_switch_on_matches_every_face(ing, gal):
    a = gal("A", vectors=[unit(0)])
    gal("B", vectors=[unit(1)])
    switch(ing["db"], True)
    ing["embed"].respond(200, json=embed_result(
        embed_face(similar_to(0, 0.8)), embed_face(similar_to(1, 0.36), box={"x": 300, "y": 20, "w": 80, "h": 100}),
        embed_face(similar_to(2, 0.9), box={"x": 500, "y": 20, "w": 80, "h": 100})))
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    assert ing["embed"].call_count == 1 and ing["detect"].call_count == 0
    assert ing["embed"].calls.last.request.url.params["crops"] == "true"
    (ev,), dets = stored(ing)
    assert ev.recognition_state == "done" and ev.recognizer == "sface 2021dec"
    assert [(d.outcome, d.person_id) for d in dets] == [("AUTHORIZED", a), ("INCONCLUSIVE", None),
                                                        ("UNRECOGNIZED", None)]
    assert dets[0].similarity == pytest.approx(0.8, abs=1e-4)
    assert (dets[0].accept_threshold, dets[0].reject_threshold) == (0.45, 0.30)


def test_switch_on_without_ready_embedder_detects_only(ing, gal):
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["models"].respond(200, json=SFACE_MISSING)
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    assert ing["embed"].call_count == 0 and ing["detect"].call_count == 1
    (ev,), (det,) = stored(ing)
    assert ev.recognition_state == "unavailable" and det.outcome == "NOT_MATCHED"


def test_embedder_lost_mid_poll_falls_back_to_detection(ing, gal):
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["embed"].respond(503, json={"error": "model_unavailable", "detail": "x", "model": "sface"})
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    (ev,), (det,) = stored(ing)
    assert ing["detect"].call_count == 1 and ev.recognition_state == "unavailable" and det.outcome == "NOT_MATCHED"
    assert ev.status == "faces_detected"


def test_detector_lost_on_embed_is_a_temporary_failure(ing, gal):
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["embed"].respond(503, json={"error": "model_unavailable", "detail": "x", "model": "yunet"})
    ing["frigate"].events = [event(1)]
    stats = asyncio.run(ing["ingestor"].ingest_once())
    assert stats.stopped == "temporary_failure" and stored(ing)[0] == []


def test_face_without_embedding_is_system_error(ing, gal):
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    ing["embed"].respond(200, json=embed_result(embed_face(None)))
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    assert stored(ing)[1][0].outcome == "SYSTEM_ERROR"


def test_event_embeddings_are_not_stored(ing, gal):
    gal("A", vectors=[unit(0)])
    switch(ing["db"], True)
    vector = similar_to(0, 0.8)
    ing["embed"].respond(200, json=embed_result(embed_face(vector)))
    ing["frigate"].events = [event(1)]
    asyncio.run(ing["ingestor"].ingest_once())
    raw = Path(ing["db"].engine.url.database).read_bytes()
    wal = Path(ing["db"].engine.url.database + "-wal")
    raw += wal.read_bytes() if wal.exists() else b""
    assert struct.pack("<128f", *vector)[:64] not in raw and b64(vector) not in raw.decode("latin-1")
    assert {c.name for c in FaceDetection.__table__.columns}.isdisjoint({"embedding", "embedding_enc"})


# --- API: outcomes, filters, deletion --------------------------------------------------------

@pytest.fixture
def matched(signed_in, settings, gal):
    """Two events stored by the real ingestor: one AUTHORIZED for person A, one UNRECOGNIZED."""
    a = gal("Asha", vectors=[unit(0)])
    switch(gal.db, True)
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        fr = FakeFrigate(router)
        router.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
        answers = iter([embed_result(embed_face(similar_to(0, 0.9))), embed_result(embed_face(similar_to(0, 0.1)))])
        router.post(f"{INFERENCE}/v1/embed").mock(side_effect=lambda req: httpx.Response(200, json=next(answers)))
        fr.events = [event(1), event(2)]
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), gal.db.sessionmaker,
                               MediaStore(settings.media_root, "faces"), cipher=gal.cipher, gallery_store=gal.store)
        asyncio.run(ingestor.ingest_once())
    return a


def test_api_shows_outcomes_and_filters(signed_in, matched):
    items = signed_in.get("/api/face-events").json()["items"]
    newest, oldest = items
    assert oldest["recognition_state"] == "done" and oldest["recognizer"] == "sface 2021dec"
    rec = oldest["detections"][0]["recognition"]
    assert rec["outcome"] == "AUTHORIZED" and rec["person"] == {"id": matched, "display_name": "Asha"}
    assert rec["reason"] == "match"
    assert newest["detections"][0]["recognition"]["reason"] == "below_reject_threshold"
    assert rec["similarity"] == pytest.approx(0.9, abs=1e-4) and rec["thresholds"] == {"accept": 0.45, "reject": 0.3}
    assert newest["detections"][0]["recognition"]["outcome"] == "UNRECOGNIZED"
    assert newest["detections"][0]["recognition"]["person"] is None
    text = json.dumps(items)
    assert "embedding" not in text
    only = signed_in.get(f"/api/face-events?person_id={matched}").json()["items"]
    assert [e["id"] for e in only] == [oldest["id"]]
    assert [e["id"] for e in signed_in.get("/api/face-events?outcome=UNRECOGNIZED").json()["items"]] == [newest["id"]]
    assert signed_in.get("/api/face-events?outcome=MAYBE").status_code == 422
    assert signed_in.get("/api/face-events?person_id=../x").status_code == 422


def test_deleting_a_person_unlinks_past_matches(signed_in, matched, gal):
    resp = signed_in.request("DELETE", f"/api/people/{matched}", json={"password": ADMIN_PASSWORD})
    assert resp.status_code == 204
    items = signed_in.get("/api/face-events").json()["items"]
    rec = items[1]["detections"][0]["recognition"]
    assert rec["outcome"] == "AUTHORIZED" and rec["person"] is None  # kept as a fact, no longer naming anyone
    assert signed_in.get(f"/api/face-events?person_id={matched}").json()["items"] == []
    with gal.db.sessionmaker() as s:
        detail = s.query(AuditEntry).filter_by(action="person_deleted").one().detail
    assert "1 past match(es) unlinked" in detail


@pytest.mark.parametrize("outcome, similarity, state, reason", [
    ("AUTHORIZED", 0.8, "done", "match"),
    ("UNRECOGNIZED", 0.1, "done", "below_reject_threshold"),
    ("UNRECOGNIZED", None, "done", "gallery_empty"),
    ("INCONCLUSIVE", 0.38, "done", "between_thresholds"),
    ("INCONCLUSIVE", 0.7, "done", "several_people_match"),
    ("INCONCLUSIVE", None, "done", "insufficient_quality"),
    ("SYSTEM_ERROR", None, "done", "recognition_error"),
    ("NOT_MATCHED", None, "off", "recognition_off"),
    ("NOT_MATCHED", None, "unavailable", "recognition_unavailable"),
    ("NOT_MATCHED", None, None, "not_performed"),
])
def test_every_outcome_has_an_explicit_reason(outcome, similarity, state, reason):
    assert rc.outcome_reason(outcome, similarity, 0.45, state) == reason


def test_api_reports_why_each_face_got_its_outcome(signed_in, settings, gal):
    """Through the real ingestor: a face too small to compare, two people matching, an in-between score."""
    gal("A", vectors=[unit(0)])
    gal("B", vectors=[unit(1)])
    switch(gal.db, True)
    both = [0.0] * 128
    both[0] = both[1] = math.sqrt(0.5)  # 0.707 to A and to B
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        fr = FakeFrigate(router)
        router.get(f"{INFERENCE}/v1/models").respond(200, json=SFACE_READY)
        router.post(f"{INFERENCE}/v1/embed").respond(200, json=embed_result(
            embed_face(similar_to(0, 0.9), box={"x": 10, "y": 20, "w": 30, "h": 36}),
            embed_face(both, box={"x": 300, "y": 20, "w": 80, "h": 100}),
            embed_face(similar_to(0, 0.38), box={"x": 500, "y": 20, "w": 80, "h": 100})))
        fr.events = [event(1)]
        asyncio.run(fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings), gal.db.sessionmaker,
                                MediaStore(settings.media_root, "faces"), cipher=gal.cipher,
                                gallery_store=gal.store).ingest_once())
    recs = [d["recognition"] for d in signed_in.get("/api/face-events").json()["items"][0]["detections"]]
    assert [(r["outcome"], r["reason"]) for r in recs] == [
        ("INCONCLUSIVE", "insufficient_quality"), ("INCONCLUSIVE", "several_people_match"),
        ("INCONCLUSIVE", "between_thresholds")]
    assert recs[0]["similarity"] is None and all(r["person"] is None for r in recs)


def test_old_events_have_no_recognition_fields(signed_in, gal):
    with gal.db.sessionmaker() as s:
        at = datetime(2026, 10, 1, tzinfo=UTC)
        ev = FaceEvent(frigate_event_id="1.0-old", camera="entrance", started_at=at, ended_at=at,
                       status="faces_detected", face_count=1)
        s.add(ev)
        s.flush()
        s.add(FaceDetection(event_id=ev.id, position=0, box_x=1, box_y=1, box_w=50, box_h=50, score=0.9,
                            keypoints="[]", quality="{}"))
        s.commit()
    item = signed_in.get("/api/face-events").json()["items"][0]
    assert item["recognition_state"] is None and item["detections"][0]["recognition"] is None


# --- real models, end to end -----------------------------------------------------------------

DATA = Path(__file__).resolve().parents[3] / "inference" / "tests" / "data" / "public_domain"
MODELS = Path(__file__).resolve().parents[3] / "inference" / "models"


@pytest.fixture
def real_inference(signed_in):
    """The real inference app (YuNet + SFace) in-process, used by the api and by an ingestor."""
    pytest.importorskip("inference_service", reason="inference package not installed in this environment")
    from inference_service.adapters.sface import SFaceEmbedder
    from inference_service.adapters.yunet import YuNetDetector
    from inference_service.config import Settings as InferenceSettings
    from inference_service.main import create_app
    from inference_service.registry import READY, SFACE, YUNET, verify_file

    if any(verify_file(spec, MODELS)[0] != READY for spec in (YUNET, SFACE)):
        pytest.skip("verified YuNet and SFace models not present (python -m inference_service.download)")
    app = create_app(InferenceSettings(models_dir=MODELS, _env_file=None))
    app.state.detector = YuNetDetector(MODELS / YUNET.file_name)  # what the lifespan does
    app.state.embedder = SFaceEmbedder(MODELS / SFACE.file_name)
    app.state.status = {YUNET.name: (READY, None), SFACE.name: (READY, None)}
    state = signed_in.app.state.app
    original = state.inference
    transport = httpx.ASGITransport(app=app)
    state.inference = InferenceClient(state.settings, transport=transport)
    yield transport
    state.inference = original


def test_real_sface_end_to_end(signed_in, settings, real_inference, tmp_path):
    """Enroll Armstrong (1964 photo) through the API, switch on, then analyse three Frigate events
    whose snapshots are Armstrong in 1969, Aldrin and Collins, with the real YuNet and SFace."""
    settings.gallery_min_face_px = 60  # the fixtures are 480x600 portraits
    person = signed_in.post("/api/people", json={"display_name": "Test Person A", "consent_date": "2026-10-30",
                                                 "consent_note": "public-domain NASA test fixture"}).json()
    enrolled = signed_in.post(f"/api/people/{person['id']}/faces", files={
        "file": ("a.jpg", (DATA / "nasa_armstrong_1964_480x600.jpg").read_bytes(), "image/jpeg")})
    assert enrolled.status_code == 201, enrolled.text
    assert enrolled.json()["embedded"] is True and enrolled.json()["embedder"] == "sface 2021dec"
    assert _put(signed_in, True).json()["enabled"] is True

    state = signed_in.app.state.app
    photos = {"armstrong_1969": "nasa_s69-31741_480x600.jpg", "aldrin": "nasa_aldrin_1969_480x600.jpg",
              "collins": "nasa_collins_s69-31742_480x600.jpg"}
    settings.face_frame_source = "snapshot"
    with respx.mock(assert_all_called=False) as router:
        router.route(host="inference.test").pass_through()
        fr = FakeFrigate(router)
        fr.events = [event(i) for i in range(len(photos))]
        names = list(photos)
        snap = router.get(url__regex=rf"{FRIGATE}/api/events/[^/]+/snapshot\.jpg")
        snap.mock(side_effect=lambda req: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=(
            DATA / photos[names[int(req.url.path.split("-ev")[1].split("/")[0])]]).read_bytes()))
        ingestor = fe.Ingestor(settings, FrigateClient(settings), InferenceClient(settings, transport=real_inference),
                               state.db.sessionmaker, MediaStore(tmp_path / "m", "faces"), cipher=state.cipher,
                               gallery_store=state.gallery_store)
        asyncio.run(ingestor.ingest_once())
    with state.db.sessionmaker() as s:
        rows = [(ev.frigate_event_id, d.outcome, d.person_id, d.similarity) for ev, d in
                s.query(FaceEvent, FaceDetection).join(FaceDetection).order_by(FaceEvent.started_at)]
    print(json.dumps({names[i]: {"outcome": o, "similarity": sim} for i, (_, o, _, sim) in enumerate(rows)}))
    assert [(o, p) for _, o, p, _ in rows] == [("AUTHORIZED", person["id"]), ("UNRECOGNIZED", None),
                                               ("UNRECOGNIZED", None)]
