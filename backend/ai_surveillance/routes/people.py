"""Authorized-person gallery: people with consent records, enrollment from uploads, deletion (ADR-0004, Phase 5).

Every route needs a signed-in session; deleting a person also needs the password again. Reference
images and their embeddings are stored encrypted; images are served only through these routes,
never cached, and embeddings never leave the server. Responses carry no file names, paths or
encrypted data. There is no route that enrolls from a face event or crop.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictBool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..captures import InsufficientStorage, check_free_space
from ..credentials import CredentialError
from ..db import AdminUser, Person, ReferenceFace, as_utc, new_id, utcnow
from ..deps import AppState, confirm_password, current_user, get_db, get_state
from ..gallery import (FACE_ENROLLED, PERSON_CREATED, PERSON_UPDATED, EnrollmentRejected, audit,
                       delete_person, delete_reference_face, image_context, single_face)
from ..inference_client import InferenceUnavailable
from ..media import describe_os_error
from ..recognition import embedder_id, is_embedder_missing, store_reference_embedding
from .analysis import PASSTHROUGH_ERRORS, _check_headers, _read_upload, error

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)

_ID = re.compile(r"^[a-f0-9]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DETECT_MAX_FACES = 10  # enough to tell "one face" from "several"


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value


def _consent_date(value: date) -> date:
    # One day of slack: the server works in UTC, the operator may be ahead of it.
    if value > datetime.now(UTC).date() + timedelta(days=1):
        raise ValueError("consent date cannot be in the future")
    if value.year < 2000:
        raise ValueError("consent date is implausibly old")
    return value


Name = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_text)]
Note = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_text)]
ConsentDate = Annotated[date, AfterValidator(_consent_date)]


class PersonCreate(BaseModel):
    display_name: Name
    notes: Note | None = None
    consent_date: ConsentDate  # when consent was given
    consent_note: Note  # where the evidence is kept, e.g. "signed form #12, HR file"


class PersonUpdate(BaseModel):
    display_name: Name | None = None
    notes: Note | None = None  # null clears the notes
    consent_date: ConsentDate | None = None
    consent_note: Note | None = None
    active: StrictBool | None = None


class PasswordConfirmation(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _face(person_id: str, f: ReferenceFace) -> dict:
    return {
        "id": f.id,
        "image": {"width": f.image_width, "height": f.image_height, "media_type": f.media_type},
        "image_url": f"/api/people/{person_id}/faces/{f.id}/image",
        "box": {"x": f.box_x, "y": f.box_y, "w": f.box_w, "h": f.box_h},
        "score": f.score,  # detection confidence: a face is there; says nothing about identity
        "keypoints5": json.loads(f.keypoints),
        "quality": json.loads(f.quality),
        "detector": f.detector,
        # Whether an embedding exists for matching, and from which model; never the embedding itself.
        "embedded": f.embedding_enc is not None,
        "embedder": f.embedder,
        "created_by": f.created_by,
        "created_at": as_utc(f.created_at).isoformat(),
    }


def _person(p: Person, face_count: int, faces: list[ReferenceFace] | None = None) -> dict:
    body = {
        "id": p.id,
        "display_name": p.display_name,
        "notes": p.notes,
        "consent": {"date": p.consent_date.isoformat(), "note": p.consent_note},
        "active": p.active,
        "face_count": face_count,
        "created_by": p.created_by,
        "created_at": as_utc(p.created_at).isoformat(),
        "updated_at": as_utc(p.updated_at).isoformat(),
    }
    if faces is not None:
        body["faces"] = [_face(p.id, f) for f in faces]
    return body


def _get(db: Session, person_id: str) -> Person:
    person = db.get(Person, person_id) if _ID.match(person_id) else None
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    return person


def _faces(db: Session, person_id: str) -> list[ReferenceFace]:
    query = select(ReferenceFace).where(ReferenceFace.person_id == person_id)
    return list(db.scalars(query.order_by(ReferenceFace.created_at, ReferenceFace.id)))


def _get_face(db: Session, person: Person, face_id: str) -> ReferenceFace:
    face = db.get(ReferenceFace, face_id) if _ID.match(face_id) else None
    if face is None or face.person_id != person.id:  # a face is only reachable through its own person
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reference face not found")
    return face


@router.get("")
def list_people(active: bool | None = None, db: Session = Depends(get_db)) -> dict:
    counts = (select(ReferenceFace.person_id, func.count().label("n"))
              .group_by(ReferenceFace.person_id).subquery())
    query = select(Person, func.coalesce(counts.c.n, 0)).outerjoin(counts, counts.c.person_id == Person.id)
    if active is not None:
        query = query.where(Person.active == active)
    rows = db.execute(query.order_by(func.lower(Person.display_name), Person.id)).all()
    return {"items": [_person(p, n) for p, n in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_person(body: PersonCreate, db: Session = Depends(get_db), user: AdminUser = Depends(current_user)) -> dict:
    now = utcnow()
    person = Person(display_name=body.display_name, notes=body.notes, consent_date=body.consent_date,
                    consent_note=body.consent_note, created_by=user.username, created_at=now, updated_at=now)
    db.add(person)
    db.flush()
    audit(db, user.username, PERSON_CREATED, "person", person.id)
    db.commit()
    log.info("Person %s created by %s", person.id, user.username)
    return _person(person, 0, [])


@router.get("/{person_id}")
def get_person(person_id: str, db: Session = Depends(get_db)) -> dict:
    person = _get(db, person_id)
    faces = _faces(db, person.id)
    return _person(person, len(faces), faces)


@router.patch("/{person_id}")
def update_person(person_id: str, body: PersonUpdate, db: Session = Depends(get_db),
                  user: AdminUser = Depends(current_user)) -> dict:
    person = _get(db, person_id)
    changed = []
    for name in sorted(body.model_fields_set):
        value = getattr(body, name)
        if value is None and name != "notes":
            raise HTTPException(422, f"{name} cannot be cleared")
        if getattr(person, name) != value:
            setattr(person, name, value)
            changed.append(name)
    if changed:
        person.updated_at = utcnow()
        # Field names only: the values may be personal data.
        audit(db, user.username, PERSON_UPDATED, "person", person.id, "changed: " + ", ".join(changed))
        db.commit()
        log.info("Person %s updated by %s (%s)", person.id, user.username, ", ".join(changed))
    faces = _faces(db, person.id)
    return _person(person, len(faces), faces)


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person_route(person_id: str, body: PasswordConfirmation, request: Request, db: Session = Depends(get_db),
                        user: AdminUser = Depends(current_user), state: AppState = Depends(get_state)) -> None:
    """Deletes the person and every reference image. Needs the signed-in user's password again."""
    person = _get(db, person_id)
    confirm_password(request, state, db, user, body.password, "person", person.id)
    username = user.username
    db.close()  # the deletion runs in its own session
    try:
        found = delete_person(state.db.sessionmaker, state.gallery_store, person_id, username)
    except OSError as exc:
        log.warning("Person %s: a reference image could not be removed: %s", person_id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not delete the reference images: {exc.strerror or type(exc).__name__}; "
                            "the person is kept") from None
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    log.info("Person %s deleted by %s", person_id, username)


@router.post("/{person_id}/faces", status_code=status.HTTP_201_CREATED)
async def enroll_face(person_id: str, request: Request, db: Session = Depends(get_db),
                      user: AdminUser = Depends(current_user), state: AppState = Depends(get_state)):
    """Enroll an uploaded reference image (multipart field `file`: JPEG, PNG or WebP) with exactly one face."""
    settings = state.settings
    person = _get(db, person_id)
    if not person.active:
        return error(409, "person_inactive", "Activate the person before enrolling reference images")
    count = db.scalar(select(func.count()).select_from(ReferenceFace).where(ReferenceFace.person_id == person.id))
    if count >= settings.gallery_max_faces_per_person:
        return error(409, "too_many_faces",
                     f"This person already has {count} reference images (limit {settings.gallery_max_faces_per_person})")
    rejected = _check_headers(request, settings.analysis_max_image_bytes)
    if rejected is not None:
        return rejected

    # Same backpressure as /api/analysis/faces: both share the inference service.
    try:
        await asyncio.wait_for(state.analysis_slots.acquire(), settings.analysis_queue_timeout_seconds)
    except TimeoutError:
        return error(503, "analysis_busy", "The analysis service is busy; try again shortly", headers={"Retry-After": "5"})
    try:
        try:
            data = await asyncio.wait_for(_read_upload(request, settings.analysis_max_image_bytes),
                                          settings.analysis_upload_timeout_seconds)
        except TimeoutError:
            return error(408, "upload_timeout", "The upload took too long")
        if isinstance(data, JSONResponse):
            return data
        try:
            # The embedding comes with the same pass when SFace is loaded; without it the image is
            # still enrolled and embedded later (recognition.embed_missing_references).
            resp = await state.inference.embed(data, settings.gallery_other_face_score, _DETECT_MAX_FACES)
            if is_embedder_missing(resp):
                resp = await state.inference.detect(data, settings.gallery_other_face_score, _DETECT_MAX_FACES)
        except InferenceUnavailable as exc:
            return error(503, "inference_unavailable", str(exc))
    finally:
        state.analysis_slots.release()

    try:
        result = resp.json()
    except ValueError:
        return error(502, "inference_failed", "The inference service returned an invalid response")
    if resp.status_code != 200 or not isinstance(result, dict):
        code = result.get("error") if isinstance(result, dict) else None
        if code in PASSTHROUGH_ERRORS:
            return error(resp.status_code, code, str(result.get("detail", ""))[:300])
        return error(502, "inference_failed", f"The inference service returned HTTP {resp.status_code}")
    try:
        candidate = single_face(result, settings)
    except EnrollmentRejected as exc:
        log.info("Enrollment for person %s rejected: %s", person.id, exc.code)
        status_code = 502 if exc.code == "inference_failed" else 422
        return error(status_code, exc.code, exc.detail, reasons=exc.reasons)

    try:
        check_free_space(state.gallery_store, settings)
    except InsufficientStorage as exc:
        return error(507, "insufficient_storage", str(exc))
    face_id, file_name = new_id(), f"{new_id()}.enc"
    blob = state.cipher.encrypt_bytes(data, image_context(face_id))
    try:
        state.gallery_store.write_atomic(file_name, blob)
    except OSError as exc:
        return error(500, "storage_failed", describe_os_error("reference image", exc))
    model = result.get("model") if isinstance(result.get("model"), dict) else {}
    face = ReferenceFace(
        id=face_id, person_id=person.id, image_file=file_name, media_type=_media_type(data),
        image_width=candidate.width, image_height=candidate.height,
        box_x=candidate.box["x"], box_y=candidate.box["y"], box_w=candidate.box["w"], box_h=candidate.box["h"],
        score=candidate.score, keypoints=json.dumps(candidate.keypoints5), quality=json.dumps(candidate.quality),
        detector=" ".join(str(model[k]) for k in ("name", "version") if model.get(k)) or None,
        created_by=user.username,
    )
    embedder = embedder_id(result.get("embedder"))
    if candidate.embedding is not None and embedder:
        store_reference_embedding(db, state.cipher, face, candidate.embedding, embedder)
    person_id, username = person.id, user.username
    # The upload and the detector took a while: the person may have been deactivated or deleted, or
    # another upload may have reached the limit. End this request's read transaction (it still sees
    # the state from its start) and check again right before writing.
    db.rollback()
    refusal = _recheck(db, person_id, settings.gallery_max_faces_per_person)
    if refusal is not None:
        state.gallery_store.delete(file_name)
        return refusal
    try:
        db.add(face)
        audit(db, username, FACE_ENROLLED, "reference_face", face_id, f"person {person_id}")
        db.commit()
    except IntegrityError:  # deleted between the check and the commit
        db.rollback()
        state.gallery_store.delete(file_name)
        return error(404, "person_deleted", "The person was deleted while the image was being checked")
    except Exception:
        state.gallery_store.delete(file_name)  # no file without its row
        raise
    log.info("Reference face %s enrolled for person %s by %s", face_id, person_id, username)
    return JSONResponse(_face(person_id, face), status_code=201)


def _recheck(db: Session, person_id: str, limit: int) -> JSONResponse | None:
    person = db.get(Person, person_id)
    if person is None:
        return error(404, "person_deleted", "The person was deleted while the image was being checked")
    if not person.active:
        return error(409, "person_inactive", "The person was deactivated while the image was being checked")
    count = db.scalar(select(func.count()).select_from(ReferenceFace).where(ReferenceFace.person_id == person_id))
    if count >= limit:
        return error(409, "too_many_faces", f"This person already has {count} reference images (limit {limit})")
    return None


def _media_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/webp"  # _read_upload accepts only these three


@router.get("/{person_id}/faces/{face_id}/image")
def reference_image(person_id: str, face_id: str, db: Session = Depends(get_db), state: AppState = Depends(get_state)):
    face = _get_face(db, _get(db, person_id), face_id)
    try:
        blob = state.gallery_store.path_for(face.image_file).read_bytes()
    except (ValueError, FileNotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image missing on disk") from None
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not read the image: {exc.strerror or type(exc).__name__}") from None
    try:
        image = state.cipher.decrypt_bytes(blob, image_context(face.id))
    except CredentialError as exc:
        log.warning("Reference face %s: %s", face.id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "The reference image cannot be decrypted: the credentials key changed or the file "
                            "is damaged. Restore the previous key, or delete this image and enroll it again.") from None
    return Response(image, media_type=face.media_type, headers={"Cache-Control": "private, no-store"})


@router.delete("/{person_id}/faces/{face_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_face_route(person_id: str, face_id: str, db: Session = Depends(get_db),
                      user: AdminUser = Depends(current_user), state: AppState = Depends(get_state)) -> None:
    face = _get_face(db, _get(db, person_id), face_id)
    username = user.username
    db.close()
    try:
        found = delete_reference_face(state.db.sessionmaker, state.gallery_store, person_id, face.id, username)
    except OSError as exc:
        log.warning("Reference face %s could not be removed: %s", face_id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not delete the image: {exc.strerror or type(exc).__name__}; "
                            "the reference face is kept") from None
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reference face not found")
    log.info("Reference face %s deleted by %s", face_id, username)
