"""Authorized-person gallery API (ADR-0004, Phase 5). In-process app, real SQLite, real encrypted files.

The inference service is a respx mock returning detector-shaped responses, so these tests check the
API's rules (exactly one face, quality gate, consent, encryption, deletion, audit), not detection
itself; real YuNet detection is covered in inference/tests and test_face_detection.py.
"""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from ai_surveillance.config import Settings
from ai_surveillance.credentials import CredentialCipher, CredentialError, load_key
from ai_surveillance.db import AuditEntry, Database, Person, ReferenceFace
from ai_surveillance.inference_client import InferenceClient
from ai_surveillance.media import MediaStore

from .conftest import ADMIN_PASSWORD, ADMIN_USER, INFERENCE

JPEG = b"\xff\xd8\xff\xe0" + b"reference-jpeg-bytes" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes" * 10
NAME = "Asha Verma"
CONSENT_NOTE = "signed form #12, HR file"


def _face(x=200, y=120, w=160, h=200, score=0.95):
    return {"index": 0, "box": {"x": x, "y": y, "w": w, "h": h}, "score": score,
            "keypoints5": [[240, 190], [320, 188], [280, 230], [245, 270], [315, 268]],
            "quality": {"face_px": min(w, h), "small_face": False, "touches_edge": False, "sharpness": 180.0}}


def _detect(*faces):
    return {"status": "faces_detected" if faces else "no_faces_detected",
            "image": {"width": 640, "height": 480, "format": "image/jpeg"}, "faces": list(faces),
            "face_count": len(faces), "model": {"name": "yunet", "version": "2023mar"}, "processing_ms": 9.0}


@pytest.fixture
def detect(frigate):
    return frigate.post(f"{INFERENCE}/v1/detect")


@pytest.fixture
def db(signed_in, settings):
    database = Database(settings.database_url)
    yield database
    database.engine.dispose()


def _gallery(settings) -> MediaStore:
    return MediaStore(settings.media_root, "gallery")


def _files(settings) -> list[str]:
    return sorted(p.name for p in _gallery(settings).captures_dir.iterdir())


def _create(client, **overrides) -> dict:
    body = {"display_name": NAME, "consent_date": "2026-10-01", "consent_note": CONSENT_NOTE, **overrides}
    resp = client.post("/api/people", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _enroll(client, person_id, data=JPEG, ctype="image/jpeg"):
    return client.post(f"/api/people/{person_id}/faces", files={"file": ("photo.jpg", data, ctype)})


def _audit(db) -> list[tuple]:
    """In insertion order (rowid): two entries can share a timestamp on a coarse clock."""
    with db.sessionmaker() as s:
        return [(e.username, e.action, e.target_type, e.target_id, e.detail)
                for e in s.scalars(select(AuditEntry).order_by(text("rowid")))]


def _delete(client, person_id, password=ADMIN_PASSWORD):
    return client.request("DELETE", f"/api/people/{person_id}", json={"password": password})


# --- access -----------------------------------------------------------------------------------

def test_every_route_requires_sign_in(client, detect):
    detect.respond(200, json=_detect(_face()))
    pid = fid = "a" * 32
    for method, path in [("get", "/api/people"), ("post", "/api/people"), ("get", f"/api/people/{pid}"),
                         ("patch", f"/api/people/{pid}"), ("delete", f"/api/people/{pid}"),
                         ("post", f"/api/people/{pid}/faces"), ("get", f"/api/people/{pid}/faces/{fid}/image"),
                         ("delete", f"/api/people/{pid}/faces/{fid}"), ("get", "/api/audit-log")]:
        assert client.request(method.upper(), path).status_code == 401, path
    assert not detect.called


def test_there_is_no_enrollment_path_from_face_events(signed_in):
    """Only an upload can enroll: no route takes a face event, crop or camera frame as its source."""
    paths = signed_in.app.openapi()["paths"]
    writes = {(m.upper(), path) for path, ops in paths.items() for m in ops
              if m in {"post", "put", "patch"} and path.startswith(("/api/people", "/api/face-events"))}
    assert writes == {("POST", "/api/people"), ("PATCH", "/api/people/{person_id}"),
                      ("POST", "/api/people/{person_id}/faces")}


def test_enrollment_refuses_a_json_reference_to_an_event(signed_in, detect):
    person = _create(signed_in)
    resp = signed_in.post(f"/api/people/{person['id']}/faces", json={"face_event_id": "b" * 32})
    assert resp.status_code == 415 and resp.json()["error"] == "unsupported_format"
    assert not detect.called


# --- people and consent -----------------------------------------------------------------------

@pytest.mark.parametrize("change, fragment", [
    ({"consent_note": None}, "consent_note"),
    ({"consent_date": None}, "consent_date"),
    ({"consent_note": "   "}, "empty"),
    ({"display_name": ""}, "display_name"),
    ({"display_name": "Bad\x00Name"}, "control"),
    ({"consent_date": "2999-01-01"}, "future"),
    ({"consent_date": "1990-01-01"}, "old"),
])
def test_a_person_needs_a_name_and_a_consent_record(signed_in, db, change, fragment):
    body = {"display_name": NAME, "consent_date": "2026-10-01", "consent_note": CONSENT_NOTE}
    body.update(change)
    body = {k: v for k, v in body.items() if v is not None}
    resp = signed_in.post("/api/people", json=body)
    assert resp.status_code == 422 and fragment in resp.text
    with db.sessionmaker() as s:
        assert s.scalar(select(func.count()).select_from(Person)) == 0


def test_create_list_get_and_audit(signed_in, db):
    person = _create(signed_in, notes="  night shift  ", display_name="  Asha Verma ")
    assert person["display_name"] == NAME and person["notes"] == "night shift"
    assert person["consent"] == {"date": "2026-10-01", "note": CONSENT_NOTE}
    assert person["active"] is True and person["face_count"] == 0 and person["faces"] == []
    assert person["created_by"] == ADMIN_USER
    other = _create(signed_in, display_name="bina rao")
    assert [p["id"] for p in signed_in.get("/api/people").json()["items"]] == [person["id"], other["id"]]  # by name
    assert signed_in.get(f"/api/people/{person['id']}").json()["display_name"] == NAME
    assert _audit(db) == [(ADMIN_USER, "person_created", "person", person["id"], None),
                          (ADMIN_USER, "person_created", "person", other["id"], None)]


def test_update_audits_field_names_not_values(signed_in, db):
    person = _create(signed_in, notes="old note")
    resp = signed_in.patch(f"/api/people/{person['id']}",
                           json={"active": False, "notes": None, "consent_note": "renewed form #40"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False and body["notes"] is None and body["consent"]["note"] == "renewed form #40"
    entry = _audit(db)[-1]
    assert entry[1] == "person_updated" and entry[4] == "changed: active, consent_note, notes"
    assert "renewed" not in str(_audit(db)) and NAME not in str(_audit(db))
    assert [p["id"] for p in signed_in.get("/api/people?active=false").json()["items"]] == [person["id"]]
    assert signed_in.get("/api/people?active=true").json()["items"] == []


def test_update_with_no_change_writes_no_audit_entry(signed_in, db):
    person = _create(signed_in)
    assert signed_in.patch(f"/api/people/{person['id']}", json={"display_name": NAME}).status_code == 200
    assert [e[1] for e in _audit(db)] == ["person_created"]


@pytest.mark.parametrize("body", [{"display_name": None}, {"consent_note": None}, {"active": None}, {"active": "yes"}])
def test_required_fields_cannot_be_cleared(signed_in, body):
    person = _create(signed_in)
    assert signed_in.patch(f"/api/people/{person['id']}", json=body).status_code == 422


def test_unknown_or_malformed_person_is_404(signed_in):
    for pid in ("f" * 32, "not-an-id", "../etc"):
        assert signed_in.get(f"/api/people/{pid}").status_code == 404


# --- enrollment -------------------------------------------------------------------------------

def test_enroll_one_face_stores_an_encrypted_image(signed_in, db, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 201, resp.text
    face = resp.json()
    assert face["box"] == {"x": 200, "y": 120, "w": 160, "h": 200} and face["score"] == 0.95
    assert face["image"] == {"width": 640, "height": 480, "media_type": "image/jpeg"}
    assert face["detector"] == "yunet 2023mar" and face["created_by"] == ADMIN_USER
    assert ".enc" not in resp.text and "image_file" not in resp.text
    sent = detect.calls.last.request
    assert sent.content == JPEG and sent.url.params["min_score"] == "0.5" and sent.url.params["max_faces"] == "10"

    [name] = _files(settings)
    stored = (_gallery(settings).captures_dir / name).read_bytes()
    assert JPEG not in stored and b"reference-jpeg-bytes" not in stored  # encrypted at rest

    image = signed_in.get(face["image_url"])
    assert image.status_code == 200 and image.content == JPEG
    assert image.headers["content-type"] == "image/jpeg" and image.headers["cache-control"] == "private, no-store"
    detail = signed_in.get(f"/api/people/{person['id']}").json()
    assert detail["face_count"] == 1 and [f["id"] for f in detail["faces"]] == [face["id"]]
    assert _audit(db)[-1] == (ADMIN_USER, "reference_face_enrolled", "reference_face", face["id"],
                              f"person {person['id']}")


def test_png_keeps_its_media_type(signed_in, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    face = _enroll(signed_in, person["id"], PNG, "image/png").json()
    image = signed_in.get(face["image_url"])
    assert image.content == PNG and image.headers["content-type"] == "image/png"


@pytest.mark.parametrize("faces, code", [
    ((), "no_face"),
    ((_face(), _face(x=420, score=0.93)), "multiple_faces"),
    ((_face(), _face(x=420, w=60, h=70, score=0.6)), "multiple_faces"),  # a weak background face still counts
])
def test_exactly_one_face_rule(signed_in, db, settings, detect, faces, code):
    detect.respond(200, json=_detect(*faces))
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 422 and resp.json()["error"] == code
    assert _files(settings) == []
    with db.sessionmaker() as s:
        assert s.scalar(select(func.count()).select_from(ReferenceFace)) == 0
    assert [e[1] for e in _audit(db)] == ["person_created"]


def test_faces_below_the_counting_floor_are_ignored(signed_in, detect):
    # The detector is asked for faces >= 0.5 only; anything lower in a response does not count.
    detect.respond(200, json=_detect(_face(), _face(x=420, score=0.3)))
    person = _create(signed_in)
    assert _enroll(signed_in, person["id"]).status_code == 201


def test_quality_gate_lists_every_reason(signed_in, settings, detect):
    detect.respond(200, json=_detect(_face(x=0, y=100, w=60, h=70, score=0.8)))
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "low_quality"
    assert body["reasons"] == ["detection score 0.80 is below 0.90", "the face is 60 px; at least 80 px is needed",
                               "the face touches the image edge"]
    assert _files(settings) == []


def test_inactive_person_and_face_limit_are_refused_before_upload(signed_in, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    signed_in.patch(f"/api/people/{person['id']}", json={"active": False})
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 409 and resp.json()["error"] == "person_inactive"
    assert not detect.called

    signed_in.patch(f"/api/people/{person['id']}", json={"active": True})
    limit = settings.gallery_max_faces_per_person
    for _ in range(limit):
        assert _enroll(signed_in, person["id"]).status_code == 201
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 409 and resp.json()["error"] == "too_many_faces"
    assert detect.call_count == limit


@pytest.mark.parametrize("status, body, expected, code", [
    (400, {"error": "invalid_image", "detail": "cannot decode"}, 400, "invalid_image"),
    (503, {"error": "model_unavailable", "detail": "yunet missing"}, 503, "model_unavailable"),
    (500, {"unexpected": True}, 502, "inference_failed"),
])
def test_inference_errors_store_nothing(signed_in, settings, detect, status, body, expected, code):
    detect.respond(status, json=body)
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == expected and resp.json()["error"] == code
    assert _files(settings) == []


def test_unreachable_inference_is_503(signed_in, settings, detect):
    detect.mock(side_effect=httpx.ConnectError("down"))
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 503 and resp.json()["error"] == "inference_unavailable"
    assert _files(settings) == []


def test_upload_checks_apply(signed_in, detect):
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"], b"GIF89a not allowed", "image/gif")
    assert resp.status_code == 415
    assert not detect.called


def test_full_disk_refuses_enrollment(signed_in, settings, detect, monkeypatch):
    detect.respond(200, json=_detect(_face()))
    settings.capture_min_free_mb = 1024
    monkeypatch.setattr(MediaStore, "free_bytes", lambda self: 10 * 1024 * 1024)
    person = _create(signed_in)
    resp = _enroll(signed_in, person["id"])
    assert resp.status_code == 507 and resp.json()["error"] == "insufficient_storage"
    assert _files(settings) == []


# --- encryption -------------------------------------------------------------------------------

def test_damaged_or_swapped_image_is_refused_without_paths(signed_in, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    first = _enroll(signed_in, person["id"]).json()
    second = _enroll(signed_in, person["id"]).json()
    with Database(settings.database_url).sessionmaker() as s:
        file_a = s.get(ReferenceFace, first["id"]).image_file
        file_b = s.get(ReferenceFace, second["id"]).image_file
    store = _gallery(settings)
    # The ciphertext is bound to its face id: another face's file does not decrypt.
    store.path_for(file_b).write_bytes(store.path_for(file_a).read_bytes())
    resp = signed_in.get(second["image_url"])
    assert resp.status_code == 500 and "cannot be decrypted" in resp.text
    assert file_b not in resp.text and "gallery" not in resp.text
    store.path_for(file_a).write_bytes(b"not encrypted")
    assert signed_in.get(first["image_url"]).status_code == 500
    store.path_for(file_a).unlink()
    assert signed_in.get(first["image_url"]).status_code == 404


def test_byte_cipher_rules(tmp_path):
    cipher = CredentialCipher(load_key(Settings(credentials_key_file=tmp_path / "k", _env_file=None)))
    blob = cipher.encrypt_bytes(b"image", "reference_face:a")
    assert cipher.decrypt_bytes(blob, "reference_face:a") == b"image"
    assert cipher.encrypt_bytes(b"image", "reference_face:a") != blob  # fresh nonce each time
    for bad, context in [(blob, "reference_face:b"), (blob[:-1] + bytes([blob[-1] ^ 1]), "reference_face:a"),
                         (blob[:20], "reference_face:a"), (b"plain", "reference_face:a")]:
        with pytest.raises(CredentialError):
            cipher.decrypt_bytes(bad, context)


# --- deletion ---------------------------------------------------------------------------------

def test_delete_person_needs_the_password(signed_in, db, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    _enroll(signed_in, person["id"])
    assert signed_in.delete(f"/api/people/{person['id']}").status_code == 422  # no body
    resp = _delete(signed_in, person["id"], "wrong-password-123")
    assert resp.status_code == 403
    assert signed_in.get("/api/auth/me").status_code == 200  # still signed in
    assert signed_in.get(f"/api/people/{person['id']}").status_code == 200 and len(_files(settings)) == 1
    assert _audit(db)[-1] == (ADMIN_USER, "password_confirmation_failed", "person", person["id"], None)


def test_delete_person_removes_files_rows_and_is_audited(signed_in, db, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    keep = _create(signed_in, display_name="Other Person")
    for _ in range(2):
        _enroll(signed_in, person["id"])
    kept_face = _enroll(signed_in, keep["id"]).json()
    assert len(_files(settings)) == 3
    assert _delete(signed_in, person["id"]).status_code == 204
    assert signed_in.get(f"/api/people/{person['id']}").status_code == 404
    assert len(_files(settings)) == 1 and signed_in.get(kept_face["image_url"]).content == JPEG
    with db.sessionmaker() as s:
        assert s.scalar(select(func.count()).select_from(ReferenceFace)) == 1
    assert _audit(db)[-1] == (ADMIN_USER, "person_deleted", "person", person["id"], "2 reference image(s) removed")
    assert _delete(signed_in, person["id"]).status_code == 404


def test_failed_file_removal_keeps_the_person(signed_in, settings, detect, monkeypatch):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    _enroll(signed_in, person["id"])

    def refuse(self, name):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(MediaStore, "delete", refuse)
    resp = _delete(signed_in, person["id"])
    assert resp.status_code == 500 and "Permission denied" in resp.text and "kept" in resp.text
    monkeypatch.undo()
    assert signed_in.get(f"/api/people/{person['id']}").json()["face_count"] == 1
    assert _delete(signed_in, person["id"]).status_code == 204 and _files(settings) == []


def test_wrong_passwords_share_the_sign_in_lockout(signed_in, settings):
    person = _create(signed_in)
    for _ in range(settings.login_max_failures):
        assert _delete(signed_in, person["id"], "wrong-password-123").status_code == 403
    assert _delete(signed_in, person["id"]).status_code == 429  # even the right password, while locked
    resp = signed_in.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert resp.status_code == 429
    assert signed_in.get(f"/api/people/{person['id']}").status_code == 200


def test_delete_one_reference_face(signed_in, db, settings, detect):
    detect.respond(200, json=_detect(_face()))
    person = _create(signed_in)
    other = _create(signed_in, display_name="Other Person")
    first = _enroll(signed_in, person["id"]).json()
    second = _enroll(signed_in, person["id"]).json()
    # A face is reachable only through its own person.
    assert signed_in.delete(f"/api/people/{other['id']}/faces/{first['id']}").status_code == 404
    assert signed_in.get(f"/api/people/{other['id']}/faces/{first['id']}/image").status_code == 404
    assert signed_in.delete(f"/api/people/{person['id']}/faces/{first['id']}").status_code == 204
    assert len(_files(settings)) == 1 and signed_in.get(second["image_url"]).status_code == 200
    assert signed_in.get(first["image_url"]).status_code == 404
    assert _audit(db)[-1] == (ADMIN_USER, "reference_face_deleted", "reference_face", first["id"],
                              f"person {person['id']}")


# --- real model -------------------------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parents[3] / "inference" / "tests" / "data" / "public_domain" / "nasa_s69-31741_480x600.jpg"
MODELS = Path(__file__).resolve().parents[3] / "inference" / "models"


@pytest.fixture
def real_detector(signed_in):
    """The api's inference client pointed at the real inference app with the real YuNet model, in-process."""
    pytest.importorskip("inference_service", reason="inference package not installed in this environment")
    from inference_service.adapters.yunet import YuNetDetector
    from inference_service.config import Settings as InferenceSettings
    from inference_service.main import create_app
    from inference_service.registry import READY, YUNET, verify_file

    if verify_file(YUNET, MODELS)[0] != READY:
        pytest.skip("verified YuNet model not present (python -m inference_service.download)")
    app = create_app(InferenceSettings(models_dir=MODELS, _env_file=None))
    app.state.detector = YuNetDetector(MODELS / YUNET.file_name)  # what the lifespan does for an injected detector
    app.state.status = {YUNET.name: (READY, None)}
    state = signed_in.app.state.app
    original = state.inference
    state.inference = InferenceClient(state.settings, transport=httpx.ASGITransport(app=app))
    yield
    state.inference = original


def test_real_yunet_enrollment_gate(signed_in, settings, real_detector):
    import cv2
    import numpy as np

    person = _create(signed_in)
    photo = FIXTURE.read_bytes()
    # The fixture's face is about 76 px (AI-ANALYSIS.md): below the default 80 px floor.
    resp = _enroll(signed_in, person["id"], photo)
    assert resp.status_code == 422 and resp.json()["error"] == "low_quality", resp.text
    assert any(" px; at least 80 px" in r for r in resp.json()["reasons"])

    image = cv2.imdecode(np.frombuffer(photo, np.uint8), cv2.IMREAD_COLOR)
    two = cv2.imencode(".jpg", cv2.hconcat([image, image]))[1].tobytes()
    resp = _enroll(signed_in, person["id"], two)
    assert resp.status_code == 422 and resp.json()["error"] == "multiple_faces", resp.text

    blank = cv2.imencode(".jpg", np.full((480, 640, 3), 128, np.uint8))[1].tobytes()
    resp = _enroll(signed_in, person["id"], blank)
    assert resp.status_code == 422 and resp.json()["error"] == "no_face", resp.text
    assert _files(settings) == []

    settings.gallery_min_face_px = 60
    resp = _enroll(signed_in, person["id"], photo)
    assert resp.status_code == 201, resp.text
    face = resp.json()
    assert abs(face["box"]["x"] - 257) <= 4 and abs(face["box"]["y"] - 115) <= 4 and face["score"] > 0.9
    assert signed_in.get(face["image_url"]).content == photo and len(_files(settings)) == 1
    print(json.dumps({"enrolled_box": face["box"], "score": face["score"]}))


# --- audit log --------------------------------------------------------------------------------

def test_audit_log_is_newest_first_and_pages(signed_in):
    ids = [_create(signed_in, display_name=f"Person {i}")["id"] for i in range(5)]
    first = signed_in.get("/api/audit-log?limit=2").json()
    second = signed_in.get(f"/api/audit-log?limit=2&cursor={first['next_cursor']}").json()
    third = signed_in.get(f"/api/audit-log?limit=2&cursor={second['next_cursor']}").json()
    items = [e for page in (first, second, third) for e in page["items"]]
    assert sorted(e["target_id"] for e in items) == sorted(ids) and third["next_cursor"] is None
    keys = [(e["at"], e["id"]) for e in items]
    assert keys == sorted(keys, reverse=True)  # newest first, ties broken by id: no gaps or repeats
    assert "Person" not in str(first)
    assert signed_in.get("/api/audit-log?cursor=bad!").status_code == 422
