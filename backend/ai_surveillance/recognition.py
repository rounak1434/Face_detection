"""Face recognition: reference embeddings, matching and the recognition switch (ADR-0004, Phase 6).

- The switch is off by default. While it is off nothing is embedded or matched: face events
  call the detector only (/v1/detect).
- Reference embeddings are computed by the inference service's SFace model (/v1/embed) from
  the encrypted reference images, and stored AES-256-GCM encrypted like the images.
- A face in an event is compared with every active person's reference faces by cosine
  similarity; the best reference face counts for each person. Outcomes:
    AUTHORIZED     best >= accept threshold and no second person also above it
    UNRECOGNIZED   best <= reject threshold, or the gallery is empty
    INCONCLUSIVE   in between, two people above accept, or the face is too small to compare
    SYSTEM_ERROR   the face had no usable embedding
    NOT_MATCHED    recognition was off or unavailable for the event
- Event embeddings are used for matching and then discarded; they are never stored.

A similarity says how alike two faces look to the model. It is not proof of identity, and the
thresholds are starting points that must be measured on the real camera.
"""

import base64
import binascii
import json
import logging
import math
import struct
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .credentials import CredentialCipher, CredentialError
from .db import AppSetting, Person, ReferenceFace, utcnow
from .gallery import image_context
from .inference_client import InferenceClient, InferenceUnavailable
from .media import MediaStore

log = logging.getLogger(__name__)

AUTHORIZED = "AUTHORIZED"
UNRECOGNIZED = "UNRECOGNIZED"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_MATCHED = "NOT_MATCHED"
SYSTEM_ERROR = "SYSTEM_ERROR"
OUTCOMES = frozenset({AUTHORIZED, UNRECOGNIZED, INCONCLUSIVE, NOT_MATCHED, SYSTEM_ERROR})

# face_events.recognition_state
STATE_OFF = "off"
STATE_UNAVAILABLE = "unavailable"
STATE_DONE = "done"

# Why a face got its outcome. Derived from what is stored (outcome, similarity, accept threshold,
# the event's recognition state), so older rows get a reason too and no migration is needed.
REASON_MATCH = "match"  # AUTHORIZED
REASON_BELOW_REJECT = "below_reject_threshold"  # UNRECOGNIZED
REASON_GALLERY_EMPTY = "gallery_empty"  # UNRECOGNIZED: nobody to compare with
REASON_BETWEEN = "between_thresholds"  # INCONCLUSIVE
REASON_TWO_PEOPLE = "several_people_match"  # INCONCLUSIVE
REASON_FACE_TOO_SMALL = "insufficient_quality"  # INCONCLUSIVE: face below RECOGNITION_MIN_FACE_PX
REASON_NO_EMBEDDING = "recognition_error"  # SYSTEM_ERROR
REASON_OFF = "recognition_off"  # NOT_MATCHED
REASON_UNAVAILABLE = "recognition_unavailable"  # NOT_MATCHED: embedder not ready
REASON_NOT_PERFORMED = "not_performed"  # NOT_MATCHED, any other case


def outcome_reason(outcome: str, similarity: float | None, accept: float | None, state: str | None) -> str:
    if outcome == AUTHORIZED:
        return REASON_MATCH
    if outcome == UNRECOGNIZED:
        return REASON_GALLERY_EMPTY if similarity is None else REASON_BELOW_REJECT
    if outcome == INCONCLUSIVE:
        if similarity is None:
            return REASON_FACE_TOO_SMALL  # match_face skips comparing faces that are too small
        return REASON_TWO_PEOPLE if accept is not None and similarity >= accept else REASON_BETWEEN
    if outcome == SYSTEM_ERROR:
        return REASON_NO_EMBEDDING
    return {STATE_OFF: REASON_OFF, STATE_UNAVAILABLE: REASON_UNAVAILABLE}.get(state or "", REASON_NOT_PERFORMED)

DIMENSIONS = 128
EMBEDDER_MODEL = "sface"  # the model name /v1/models and /v1/embed report
_SWITCH_KEY = "recognition_enabled"
_DETECT_MAX_FACES = 10
_MIN_REFERENCE_IOU = 0.5  # a re-detected reference face must overlap the enrolled box this much


# --- embeddings ------------------------------------------------------------------------------

def decode_embedding(value) -> tuple[float, ...] | None:
    """A /v1/embed embedding (base64 of 128 little-endian float32), L2-normalised; None if unusable."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return unpack(raw)


def pack(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{DIMENSIONS}f", *vector)


def unpack(raw: bytes) -> tuple[float, ...] | None:
    if len(raw) != DIMENSIONS * 4:
        return None
    vector = struct.unpack(f"<{DIMENSIONS}f", raw)
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm == 0.0:
        return None
    return tuple(v / norm for v in vector)


def embedding_context(face_id: str) -> str:
    return f"reference_embedding:{face_id}"


def embedder_id(info) -> str | None:
    """"sface 2021dec" from the `embedder` (/v1/embed) or a model entry (/v1/models)."""
    if isinstance(info, dict) and info.get("name") and info.get("version"):
        return f"{info['name']} {info['version']}"
    return None


async def current_embedder(inference: InferenceClient) -> tuple[str | None, str]:
    """(embedder id when SFace is loaded, status for operators)."""
    try:
        models = await inference.models()
    except InferenceUnavailable:
        return None, "unreachable"
    for model in models if isinstance(models, list) else []:
        if isinstance(model, dict) and model.get("name") == EMBEDDER_MODEL:
            status = str(model.get("status") or "unknown")
            return (embedder_id(model) if status == "ready" else None), status
    return None, "not_installed"


def is_embedder_missing(resp) -> bool:
    """A /v1/embed 503 because SFace (not the detector) is unavailable: fall back to detection."""
    if resp.status_code != 503:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "model_unavailable" and body.get("model") == EMBEDDER_MODEL


# --- the switch ------------------------------------------------------------------------------

def is_enabled(db: Session) -> bool:
    row = db.get(AppSetting, _SWITCH_KEY)
    try:
        return row is not None and json.loads(row.value) is True
    except ValueError:
        return False  # a damaged value fails closed: recognition stays off


def switch_record(db: Session) -> AppSetting | None:
    return db.get(AppSetting, _SWITCH_KEY)


def set_enabled(db: Session, enabled: bool, username: str) -> None:
    """Change the switch in the caller's transaction."""
    row = db.get(AppSetting, _SWITCH_KEY)
    if row is None:
        row = AppSetting(key=_SWITCH_KEY)
        db.add(row)
    row.value, row.updated_at, row.updated_by = json.dumps(enabled), utcnow(), username


# --- gallery and matching --------------------------------------------------------------------

@dataclass
class Gallery:
    embedder: str
    entries: list[tuple[str, tuple[float, ...]]] = field(default_factory=list)  # (person id, vector)
    unreadable: int = 0

    @property
    def people(self) -> int:
        return len({person for person, _ in self.entries})


@dataclass
class Match:
    outcome: str
    person_id: str | None = None
    similarity: float | None = None


def load_gallery(sessions: sessionmaker, cipher: CredentialCipher, embedder: str) -> Gallery:
    """Embeddings of active people's reference faces made by `embedder`, decrypted in memory."""
    gallery = Gallery(embedder)
    with sessions() as db:
        rows = db.execute(
            select(ReferenceFace.id, ReferenceFace.person_id, ReferenceFace.embedding_enc)
            .join(Person, Person.id == ReferenceFace.person_id)
            .where(Person.active.is_(True), ReferenceFace.embedder == embedder,
                   ReferenceFace.embedding_enc.is_not(None))
        ).all()
    for face_id, person_id, blob in rows:
        try:
            vector = unpack(cipher.decrypt_bytes(blob, embedding_context(face_id)))
        except CredentialError:
            vector = None
        if vector is None:
            gallery.unreadable += 1
            log.warning("Reference face %s: stored embedding cannot be used (key changed or damaged)", face_id)
            continue
        gallery.entries.append((person_id, vector))
    return gallery


def match(vector: tuple[float, ...], gallery: Gallery, settings: Settings) -> Match:
    best: dict[str, float] = {}
    for person_id, reference in gallery.entries:
        similarity = sum(a * b for a, b in zip(vector, reference, strict=True))
        if similarity > best.get(person_id, -2.0):
            best[person_id] = similarity
    if not best:
        return Match(UNRECOGNIZED)
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    person_id, top = ranked[0]
    top = round(top, 4)
    if top >= settings.recognition_accept_threshold:
        if len(ranked) > 1 and ranked[1][1] >= settings.recognition_accept_threshold:
            return Match(INCONCLUSIVE, similarity=top)  # two people look alike to the model: no decision
        return Match(AUTHORIZED, person_id, top)
    if top <= settings.recognition_reject_threshold:
        return Match(UNRECOGNIZED, similarity=top)
    return Match(INCONCLUSIVE, similarity=top)


def match_face(box: dict, embedding: tuple[float, ...] | None, gallery: Gallery, settings: Settings) -> Match:
    if min(int(box["w"]), int(box["h"])) < settings.recognition_min_face_px:
        return Match(INCONCLUSIVE)  # too small for a meaningful comparison
    if embedding is None:
        return Match(SYSTEM_ERROR)
    return match(embedding, gallery, settings)


# --- reference embeddings --------------------------------------------------------------------

def _iou(a: dict, b: dict) -> float:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1, y1 = min(a["x"] + a["w"], b["x"] + b["w"]), min(a["y"] + a["h"], b["y"] + b["h"])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def reference_vector(result: dict, box: dict) -> tuple[float, ...] | None:
    """The embedding of the face in a /v1/embed result that is the enrolled face (box overlap)."""
    best, best_iou = None, _MIN_REFERENCE_IOU
    for face in result.get("faces") or []:
        fbox = face.get("box") if isinstance(face, dict) else None
        if not isinstance(fbox, dict) or not all(isinstance(fbox.get(k), int | float) for k in ("x", "y", "w", "h")):
            continue
        overlap = _iou(fbox, box)
        if overlap >= best_iou:
            best, best_iou = face, overlap
    return decode_embedding(best.get("embedding")) if best else None


def store_reference_embedding(db: Session, cipher: CredentialCipher, face: ReferenceFace,
                              vector: tuple[float, ...], embedder: str) -> None:
    face.embedding_enc = cipher.encrypt_bytes(pack(vector), embedding_context(face.id))
    face.embedder = embedder


async def embed_missing_references(sessions: sessionmaker, inference: InferenceClient, cipher: CredentialCipher,
                                   store: MediaStore, settings: Settings, embedder: str,
                                   skip: set[str] | None = None) -> tuple[int, int]:
    """Embed reference faces that have no embedding from `embedder` yet. Returns (done, failed).

    Raises InferenceUnavailable when the service or SFace is unavailable (try again later). A face
    that cannot be embedded (unreadable file, face not found again) is counted as failed and added
    to `skip`, so it is not retried on every poll.
    """
    skip = skip if skip is not None else set()
    with sessions() as db:
        faces = [f for f in db.scalars(select(ReferenceFace).where(
            or_(ReferenceFace.embedding_enc.is_(None), ReferenceFace.embedder.is_(None),
                ReferenceFace.embedder != embedder))) if f.id not in skip]
    done = failed = 0
    for face in faces:
        box = {"x": face.box_x, "y": face.box_y, "w": face.box_w, "h": face.box_h}
        try:
            image = cipher.decrypt_bytes(store.path_for(face.image_file).read_bytes(), image_context(face.id))
        except (OSError, ValueError, CredentialError) as exc:
            log.warning("Reference face %s cannot be embedded: image unreadable (%s)", face.id, type(exc).__name__)
            skip.add(face.id)
            failed += 1
            continue
        resp = await inference.embed(image, settings.gallery_other_face_score, _DETECT_MAX_FACES)
        if resp.status_code == 503:
            raise InferenceUnavailable("The face embedder is unavailable")
        try:
            result = resp.json() if resp.status_code == 200 else None
        except ValueError:
            result = None
        vector = reference_vector(result, box) if isinstance(result, dict) else None
        got = embedder_id(result.get("embedder")) if isinstance(result, dict) else None
        if vector is None or got != embedder:
            log.warning("Reference face %s cannot be embedded: HTTP %s, face %s", face.id, resp.status_code,
                        "not found again" if vector is None else f"embedded by {got}")
            skip.add(face.id)
            failed += 1
            continue
        with sessions() as db:
            row = db.get(ReferenceFace, face.id)
            if row is None:
                continue  # deleted meanwhile
            store_reference_embedding(db, cipher, row, vector, embedder)
            db.commit()
        done += 1
    if done or failed:
        log.info("Reference embeddings: %d computed, %d could not be computed", done, failed)
    return done, failed
