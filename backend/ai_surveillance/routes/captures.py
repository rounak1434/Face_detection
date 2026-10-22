import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..captures import create_clip_request, run_clip_export, take_snapshot
from ..db import Capture, as_utc
from ..deps import AppState, current_user, get_db, get_state, known_camera

router = APIRouter(prefix="/api", tags=["captures"], dependencies=[Depends(current_user)])


class ClipRequest(BaseModel):
    seconds: int | None = None


def _to_dict(c: Capture) -> dict:
    return {
        "id": c.id,
        "camera": c.camera,
        "kind": c.kind,
        "status": c.status,
        # SQLite returns naive datetimes; they are stored in UTC, so say so.
        "created_at": as_utc(c.created_at).isoformat(),
        "size_bytes": c.size_bytes,
        "duration_seconds": c.duration_seconds,
        "error": c.error,
        "file_url": f"/api/captures/{c.id}/file" if c.status == "ready" else None,
    }


@router.post("/cameras/{camera}/snapshot", status_code=status.HTTP_201_CREATED)
async def snapshot(
    camera: str = Depends(known_camera),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> dict:
    capture = await take_snapshot(db, state.frigate, state.store, camera)
    if capture.status == "failed":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, capture.error or "Snapshot failed")
    return _to_dict(capture)


@router.post("/cameras/{camera}/clip", status_code=status.HTTP_202_ACCEPTED)
async def clip(
    body: ClipRequest | None = None,
    camera: str = Depends(known_camera),
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> dict:
    settings = state.settings
    seconds = body.seconds if body and body.seconds is not None else settings.clip_default_seconds
    if not 1 <= seconds <= settings.clip_max_seconds:
        raise HTTPException(422, f"seconds must be 1-{settings.clip_max_seconds}")
    capture = create_clip_request(db, camera, seconds)
    state.spawn(
        run_clip_export(capture.id, int(time.time()), settings, state.frigate, state.store, state.db.sessionmaker)
    )
    return _to_dict(capture)


@router.get("/captures")
def list_captures(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    limit = max(1, min(limit, 200))
    rows = db.scalars(select(Capture).order_by(Capture.created_at.desc()).limit(limit))
    return [_to_dict(c) for c in rows]


@router.get("/captures/{capture_id}")
def get_capture(capture_id: str, db: Session = Depends(get_db)) -> dict:
    capture = db.get(Capture, capture_id)
    if capture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capture not found")
    return _to_dict(capture)


@router.get("/captures/{capture_id}/file")
def capture_file(capture_id: str, db: Session = Depends(get_db), state: AppState = Depends(get_state)):
    capture = db.get(Capture, capture_id)
    if capture is None or capture.status != "ready" or not capture.file_name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capture file not available")
    try:
        path = state.store.path_for(capture.file_name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capture file not available") from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capture file missing on disk")
    media_type = "video/mp4" if capture.kind == "clip" else "image/jpeg"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})
