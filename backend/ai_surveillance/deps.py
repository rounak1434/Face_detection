"""Shared application state and FastAPI dependencies."""

import asyncio
import time
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import Settings
from .credentials import CredentialCipher
from .db import AdminUser, Database
from .frigate_client import FrigateClient, FrigateError
from .gallery import PASSWORD_CONFIRMATION_FAILED, audit
from .inference_client import InferenceClient
from .media import MediaStore
from .security import SESSION_COOKIE, LoginThrottle, user_for_token, verify_password

CAMERA_CACHE_SECONDS = 30.0


@dataclass
class AppState:
    settings: Settings
    db: Database
    frigate: FrigateClient
    store: MediaStore
    throttle: LoginThrottle
    cipher: CredentialCipher
    inference: InferenceClient
    analysis_slots: asyncio.Semaphore
    face_store: MediaStore | None = None  # media/faces: face-event frames and crops
    gallery_store: MediaStore | None = None  # media/gallery: encrypted reference images
    background: set[asyncio.Task] = field(default_factory=set)
    device_tests: dict[str, float] = field(default_factory=dict)  # device id -> last test time
    frigate_apply_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one "Apply to Frigate" at a time
    _cameras: dict[str, dict] | None = None
    _cameras_at: float = 0.0

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def cameras(self) -> dict[str, dict]:
        now = time.monotonic()
        if self._cameras is None or now - self._cameras_at > CAMERA_CACHE_SECONDS:
            self._cameras = await self.frigate.cameras()
            self._cameras_at = now
        return self._cameras


def get_state(request: Request) -> AppState:
    return request.app.state.app


def get_db(request: Request):
    yield from get_state(request).db.session()


def client_key(request: Request) -> str:
    """The key failed password attempts are counted under (sign-in and re-confirmation alike)."""
    return request.client.host if request.client else "unknown"


def current_user(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    user = user_for_token(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    return user


def confirm_password(request: Request, state: AppState, db: Session, user: AdminUser, password: str,
                     target_type: str, target_id: str) -> None:
    """Re-check the signed-in user's password before a destructive or sensitive change.

    Failures count towards the same per-client lockout as sign-in and are audited. 403, not 401:
    the session stays valid, only this request is refused.
    """
    key = client_key(request)
    if state.throttle.is_locked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again later.")
    if not verify_password(user.password_hash, password):
        state.throttle.record_failure(key)
        audit(db, user.username, PASSWORD_CONFIRMATION_FAILED, target_type, target_id)
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password is incorrect")
    state.throttle.reset(key)


async def known_camera(camera: str, state: AppState = Depends(get_state)) -> str:
    """Only cameras configured in Frigate are accepted as path parameters."""
    try:
        cameras = await state.cameras()
    except FrigateError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if camera not in cameras:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown camera")
    return camera
