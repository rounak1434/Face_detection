"""The recognition switch (ADR-0004, Phase 6).

Off by default. Switching either way needs the signed-in user's password again and is audited.
Switching on is refused while the SFace embedder is not ready or no active person has a usable
reference embedding; reference faces without an embedding are embedded first. Nothing is
matched retroactively: the switch applies to face events analysed from then on.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import recognition
from ..db import AdminUser, Person, ReferenceFace, as_utc
from ..deps import AppState, confirm_password, current_user, get_db, get_state
from ..gallery import RECOGNITION_DISABLED, RECOGNITION_ENABLED, audit
from ..inference_client import InferenceUnavailable
from .analysis import error

router = APIRouter(prefix="/api/recognition", tags=["recognition"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)


class SwitchRequest(BaseModel):
    enabled: StrictBool
    password: str = Field(min_length=1, max_length=256)


def _payload(db: Session, state: AppState, embedder: str | None, embedder_status: str,
             gallery: recognition.Gallery | None = None) -> dict:
    settings = state.settings
    record = recognition.switch_record(db)
    embedded = select(func.count()).select_from(ReferenceFace).join(Person, Person.id == ReferenceFace.person_id) \
        .where(Person.active.is_(True), ReferenceFace.embedding_enc.is_not(None), ReferenceFace.embedder == embedder)
    people = select(func.count(func.distinct(ReferenceFace.person_id))).join(Person, Person.id == ReferenceFace.person_id) \
        .where(Person.active.is_(True), ReferenceFace.embedding_enc.is_not(None), ReferenceFace.embedder == embedder)
    pending = select(func.count()).select_from(ReferenceFace).where(
        (ReferenceFace.embedding_enc.is_(None)) | (ReferenceFace.embedder.is_(None))
        | (ReferenceFace.embedder != (embedder or "")))
    return {
        "enabled": recognition.is_enabled(db),
        "updated_at": as_utc(record.updated_at).isoformat() if record else None,
        "updated_by": record.updated_by if record else None,
        "embedder": {"ready": embedder is not None, "model": embedder, "status": embedder_status},
        "gallery": {
            "people": db.scalar(people) if embedder else 0,  # active people with a usable embedding
            "reference_faces": db.scalar(embedded) if embedder else 0,
            "pending": db.scalar(pending),  # not yet embedded by the current model
            "unreadable": gallery.unreadable if gallery else None,
        },
        "thresholds": {"accept": settings.recognition_accept_threshold, "reject": settings.recognition_reject_threshold,
                       "min_face_px": settings.recognition_min_face_px},
    }


@router.get("")
async def get_recognition(db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    embedder, status = await recognition.current_embedder(state.inference)
    return _payload(db, state, embedder, status)


@router.put("")
async def set_recognition(body: SwitchRequest, request: Request, db: Session = Depends(get_db),
                          user: AdminUser = Depends(current_user), state: AppState = Depends(get_state)):
    confirm_password(request, state, db, user, body.password, "setting", "recognition")
    embedder, status = await recognition.current_embedder(state.inference)
    gallery = None
    if body.enabled:
        if embedder is None:
            return error(409, "embedder_unavailable",
                         f"Recognition cannot be switched on: the face embedder (SFace) is not ready ({status})")
        try:
            await recognition.embed_missing_references(state.db.sessionmaker, state.inference, state.cipher,
                                                       state.gallery_store, state.settings, embedder)
        except InferenceUnavailable as exc:
            return error(409, "embedder_unavailable", f"Recognition cannot be switched on: {exc}")
        gallery = recognition.load_gallery(state.db.sessionmaker, state.cipher, embedder)
        if gallery.people == 0:
            return error(409, "gallery_empty",
                         "Recognition cannot be switched on: no active person has a usable reference face")
        db.expire_all()  # embeddings were written in other sessions
    if recognition.is_enabled(db) != body.enabled:
        recognition.set_enabled(db, body.enabled, user.username)
        audit(db, user.username, RECOGNITION_ENABLED if body.enabled else RECOGNITION_DISABLED, "setting",
              "recognition", f"thresholds accept {state.settings.recognition_accept_threshold}, "
                             f"reject {state.settings.recognition_reject_threshold}; embedder {embedder or status}")
        db.commit()
        log.info("Recognition switched %s by %s", "on" if body.enabled else "off", user.username)
    return JSONResponse(_payload(db, state, embedder, status, gallery))
