from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import AdminUser
from ..deps import AppState, current_user, get_db, get_state
from ..security import SESSION_COOKIE, authenticate, create_session, revoke_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
) -> dict:
    key = _client_key(request)
    if state.throttle.is_locked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again later.")
    user = authenticate(db, body.username, body.password)
    if user is None:
        state.throttle.record_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    state.throttle.reset(key)
    token = create_session(db, user, state.settings.session_ttl_hours)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=state.settings.session_ttl_hours * 3600,
        httponly=True,
        secure=state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"username": user.username}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    revoke_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me")
def me(user: AdminUser = Depends(current_user)) -> dict:
    return {"username": user.username}
