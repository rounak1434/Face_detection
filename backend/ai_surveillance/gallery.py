"""Authorized-person gallery: enrollment checks, encrypted reference images, deletion, audit (ADR-0004, Phase 5).

Enrollment accepts only an image that a signed-in administrator uploads. Nothing here reads face
events, Frigate snapshots or camera frames: there is deliberately no path from footage into the
gallery. Matching against the gallery (Phase 6) is in recognition.py and runs only while the
recognition switch is on.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import AuditEntry, FaceDetection, Person, ReferenceFace
from .face_detection import _valid_face
from .media import MediaStore

log = logging.getLogger(__name__)

# Audit actions. Entries hold ids and field names only: never names, notes, consent text or images.
PERSON_CREATED = "person_created"
PERSON_UPDATED = "person_updated"
PERSON_DELETED = "person_deleted"
FACE_ENROLLED = "reference_face_enrolled"
FACE_DELETED = "reference_face_deleted"
PASSWORD_CONFIRMATION_FAILED = "password_confirmation_failed"
RECOGNITION_ENABLED = "recognition_enabled"
RECOGNITION_DISABLED = "recognition_disabled"


def audit(db: Session, username: str, action: str, target_type: str | None = None, target_id: str | None = None,
          detail: str | None = None) -> None:
    """Add an audit entry to the caller's transaction; it is committed with the change it describes."""
    db.add(AuditEntry(username=username, action=action, target_type=target_type, target_id=target_id,
                      detail=detail[:500] if detail else None))


def image_context(face_id: str) -> str:
    """Associated data for a reference image: a file copied onto another face's record fails to decrypt."""
    return f"reference_face:{face_id}"


class EnrollmentRejected(Exception):
    def __init__(self, code: str, detail: str, reasons: list[str] | None = None) -> None:
        super().__init__(detail)
        self.code, self.detail, self.reasons = code, detail, reasons or []


@dataclass
class ReferenceCandidate:
    box: dict  # x, y, w, h in image pixels
    score: float
    keypoints5: list[list[float]]
    quality: dict
    width: int
    height: int
    embedding: tuple[float, ...] | None = None  # when the upload went through /v1/embed


def single_face(result: dict, settings: Settings) -> ReferenceCandidate:
    """The one usable face in a detector response, or EnrollmentRejected.

    The detector is asked for every face down to gallery_other_face_score, so a second, weaker
    face (a person in the background, a photo on the wall) still makes the image unusable.
    """
    image = result.get("image") if isinstance(result.get("image"), dict) else {}
    width, height = image.get("width"), image.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        raise EnrollmentRejected("inference_failed", "The face detector returned no image size")
    faces = [f for f in result.get("faces") or []
             if _valid_face(f) and f["score"] >= settings.gallery_other_face_score]
    if not faces:
        raise EnrollmentRejected("no_face", "No face was found in the image")
    if len(faces) > 1:
        raise EnrollmentRejected("multiple_faces",
                                 f"{len(faces)} faces were found; a reference image must show exactly one face")
    face = faces[0]
    box = {k: int(face["box"][k]) for k in ("x", "y", "w", "h")}
    reasons = []
    if face["score"] < settings.gallery_min_score:
        reasons.append(f"detection score {face['score']:.2f} is below {settings.gallery_min_score:.2f}")
    face_px = min(box["w"], box["h"])
    if face_px < settings.gallery_min_face_px:
        reasons.append(f"the face is {face_px} px; at least {settings.gallery_min_face_px} px is needed")
    if box["x"] <= 0 or box["y"] <= 0 or box["x"] + box["w"] >= width or box["y"] + box["h"] >= height:
        reasons.append("the face touches the image edge")
    if reasons:
        raise EnrollmentRejected("low_quality", "The face is not usable as a reference: " + "; ".join(reasons),
                                 reasons)
    quality = face.get("quality") if isinstance(face.get("quality"), dict) else {}
    from .recognition import decode_embedding  # recognition imports this module

    return ReferenceCandidate(box, float(face["score"]), face["keypoints5"], quality, width, height,
                              decode_embedding(face.get("embedding")))


def _remove_files(store: MediaStore, names: list[str]) -> None:
    """Raises OSError if a file cannot be removed, so the caller keeps the rows (nothing orphaned on disk)."""
    for name in names:
        try:
            store.delete(name)
        except ValueError:
            pass  # not a name we generate: no file of ours


def delete_person(sessions: sessionmaker, store: MediaStore, person_id: str, username: str) -> bool:
    """Delete a person: image files first, then the rows (reference faces cascade), with an audit entry."""
    with sessions() as db:
        if db.get(Person, person_id) is None:
            return False
        files = list(db.scalars(select(ReferenceFace.image_file).where(ReferenceFace.person_id == person_id)))
        _remove_files(store, files)
        # Past matches keep their outcome and similarity but no longer name the person.
        unlinked = db.execute(update(FaceDetection).where(FaceDetection.person_id == person_id)
                              .values(person_id=None)).rowcount or 0
        db.execute(delete(ReferenceFace).where(ReferenceFace.person_id == person_id))
        db.execute(delete(Person).where(Person.id == person_id))
        audit(db, username, PERSON_DELETED, "person", person_id,
              f"{len(files)} reference image(s) and their embeddings removed; {unlinked} past match(es) unlinked")
        db.commit()
    return True


def delete_reference_face(sessions: sessionmaker, store: MediaStore, person_id: str, face_id: str,
                          username: str) -> bool:
    with sessions() as db:
        face = db.get(ReferenceFace, face_id)
        if face is None or face.person_id != person_id:
            return False
        _remove_files(store, [face.image_file])
        db.execute(delete(ReferenceFace).where(ReferenceFace.id == face_id))
        audit(db, username, FACE_DELETED, "reference_face", face_id, f"person {person_id}")
        db.commit()
    return True
