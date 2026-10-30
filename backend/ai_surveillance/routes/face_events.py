"""Face event history: list, filter, view, images, delete (ADR-0004, Phase 4).

Face events record where faces were and, for events analysed while recognition was
on (Phase 6), each face's match outcome and matched person. Every route needs a
signed-in session. Responses never contain file names, paths or embeddings.
"""

import base64
import binascii
import json
import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from ..db import AdminUser, FaceDetection, FaceEvent, Person, as_utc
from ..deps import AppState, current_user, get_db, get_state
from ..face_detection import FACE_EVENT_STATUSES
from ..face_events import delete_face_event
from ..recognition import OUTCOMES, outcome_reason

router = APIRouter(prefix="/api/face-events", tags=["face-events"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)

_ID = re.compile(r"^[a-f0-9]{32}$")
_NOT_FOUND = "Face event not found"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _encode_cursor(event: FaceEvent) -> str:
    raw = json.dumps([as_utc(event.started_at).isoformat(), event.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        started, event_id = json.loads(raw)
        if not (isinstance(started, str) and isinstance(event_id, str) and _ID.match(event_id)):
            raise ValueError
        return _utc(datetime.fromisoformat(started)), event_id
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid cursor") from None


def _recognition(d: FaceDetection, names: dict[str, str], state: str | None) -> dict | None:
    if d.outcome is None:
        return None  # stored before recognition existed
    return {
        "outcome": d.outcome,
        # Why: match, below_reject_threshold, gallery_empty, between_thresholds, several_people_match,
        # insufficient_quality, recognition_error, recognition_off, recognition_unavailable, not_performed.
        "reason": outcome_reason(d.outcome, d.similarity, d.accept_threshold, state),
        # The matched person (AUTHORIZED only); null also when that person was deleted since.
        "person": {"id": d.person_id, "display_name": names.get(d.person_id)} if d.person_id else None,
        "similarity": d.similarity,  # how alike the faces look to the model, not proof of identity
        "thresholds": {"accept": d.accept_threshold, "reject": d.reject_threshold}
        if d.accept_threshold is not None else None,
    }


def _detection(event_id: str, d: FaceDetection, names: dict[str, str], state: str | None) -> dict:
    return {
        "id": d.id,
        "position": d.position,
        "box": {"x": d.box_x, "y": d.box_y, "w": d.box_w, "h": d.box_h},
        "score": d.score,  # detection confidence: a face is there; says nothing about identity
        "keypoints5": json.loads(d.keypoints),
        "quality": json.loads(d.quality),
        "crop_url": f"/api/face-events/{event_id}/detections/{d.id}/crop" if d.crop_file else None,
        "recognition": _recognition(d, names, state),
    }


def _names(db: Session, groups) -> dict[str, str]:
    ids = {d.person_id for detections in groups for d in detections if d.person_id}
    return dict(db.execute(select(Person.id, Person.display_name).where(Person.id.in_(ids))).all()) if ids else {}


def _event(e: FaceEvent, detections: list[FaceDetection], names: dict[str, str]) -> dict:
    return {
        "id": e.id,
        "frigate_event_id": e.frigate_event_id,
        "camera": e.camera,
        "started_at": as_utc(e.started_at).isoformat(),
        "ended_at": as_utc(e.ended_at).isoformat(),
        "status": e.status,
        "face_count": e.face_count,
        "frame_source": e.frame_source,
        "frame": {"width": e.frame_width, "height": e.frame_height} if e.frame_width else None,
        "frame_url": f"/api/face-events/{e.id}/frame" if e.frame_file else None,
        "detector": e.detector,
        "detail": e.detail,
        "recognition_state": e.recognition_state,  # off | unavailable | done; null before recognition existed
        "recognizer": e.recognizer,
        "detections": [_detection(e.id, d, names, e.recognition_state) for d in detections],
    }


def _detections_by_event(db: Session, ids: list[str]) -> dict[str, list[FaceDetection]]:
    grouped: dict[str, list[FaceDetection]] = {i: [] for i in ids}
    if ids:
        query = select(FaceDetection).where(FaceDetection.event_id.in_(ids)).order_by(FaceDetection.position)
        for d in db.scalars(query):
            grouped[d.event_id].append(d)
    return grouped


def _get(db: Session, event_id: str) -> FaceEvent:
    event = db.get(FaceEvent, event_id) if _ID.match(event_id) else None
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return event


def _image(state: AppState, file_name: str | None):
    if not file_name or state.face_store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not available")
    try:
        path = state.face_store.path_for(file_name)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not available") from None
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image missing on disk")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.get("")
def list_face_events(
    camera: str | None = Query(default=None, max_length=64),
    status_: str | None = Query(default=None, alias="status"),
    has_faces: bool | None = None,
    since: datetime | None = Query(default=None, description="started at or after (ISO 8601; UTC if no offset)"),
    until: datetime | None = Query(default=None, description="started before (ISO 8601; UTC if no offset)"),
    outcome: str | None = Query(default=None, description="events with at least one face with this outcome"),
    person_id: str | None = Query(default=None, description="events where this person was recognized"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=256),
    db: Session = Depends(get_db),
) -> dict:
    """Newest first. `next_cursor` continues the same query; it stays valid while events arrive."""
    if status_ is not None and status_ not in FACE_EVENT_STATUSES:
        raise HTTPException(422,
                            f"status must be one of: {', '.join(sorted(FACE_EVENT_STATUSES))}")
    query = select(FaceEvent)
    if camera:
        query = query.where(FaceEvent.camera == camera)
    if status_:
        query = query.where(FaceEvent.status == status_)
    if has_faces is not None:
        query = query.where(FaceEvent.face_count > 0 if has_faces else FaceEvent.face_count == 0)
    if since:
        query = query.where(FaceEvent.started_at >= _utc(since))
    if until:
        query = query.where(FaceEvent.started_at < _utc(until))
    if outcome is not None:
        if outcome not in OUTCOMES:
            raise HTTPException(422, f"outcome must be one of: {', '.join(sorted(OUTCOMES))}")
        query = query.where(exists().where(FaceDetection.event_id == FaceEvent.id, FaceDetection.outcome == outcome))
    if person_id is not None:
        if not _ID.match(person_id):
            raise HTTPException(422, "Invalid person_id")
        query = query.where(exists().where(FaceDetection.event_id == FaceEvent.id, FaceDetection.person_id == person_id))
    if cursor:
        started, event_id = _decode_cursor(cursor)
        query = query.where(or_(FaceEvent.started_at < started,
                                and_(FaceEvent.started_at == started, FaceEvent.id < event_id)))
    rows = list(db.scalars(query.order_by(FaceEvent.started_at.desc(), FaceEvent.id.desc()).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    detections = _detections_by_event(db, [e.id for e in page])
    names = _names(db, detections.values())
    return {
        "items": [_event(e, detections[e.id], names) for e in page],
        "next_cursor": _encode_cursor(page[-1]) if more else None,
    }


@router.get("/{event_id}")
def get_face_event(event_id: str, db: Session = Depends(get_db)) -> dict:
    event = _get(db, event_id)
    detections = _detections_by_event(db, [event.id])[event.id]
    return _event(event, detections, _names(db, [detections]))


@router.get("/{event_id}/frame")
def face_event_frame(event_id: str, db: Session = Depends(get_db), state: AppState = Depends(get_state)):
    return _image(state, _get(db, event_id).frame_file)


@router.get("/{event_id}/detections/{detection_id}/crop")
def face_detection_crop(event_id: str, detection_id: str, db: Session = Depends(get_db),
                        state: AppState = Depends(get_state)):
    event = _get(db, event_id)
    detection = db.get(FaceDetection, detection_id) if _ID.match(detection_id) else None
    if detection is None or detection.event_id != event.id:  # a crop is only reachable through its own event
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Face detection not found")
    return _image(state, detection.crop_file)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_face_event_route(event_id: str, db: Session = Depends(get_db), state: AppState = Depends(get_state),
                            user: AdminUser = Depends(current_user)) -> None:
    event = _get(db, event_id)
    username = user.username
    db.close()  # the deletion runs in its own session
    try:
        found = delete_face_event(state.db.sessionmaker, state.face_store, event.id, username)
    except OSError as exc:
        log.warning("Face event %s: an image could not be removed: %s", event_id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Could not delete the images: {exc.strerror or type(exc).__name__}; "
                            "the face event is kept") from None
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    log.info("Face event %s deleted by %s", event_id, username)
