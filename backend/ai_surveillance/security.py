"""Password hashing, login sessions and brute-force protection."""

import hashlib
import secrets
import threading
import time
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import AdminUser, LoginSession, as_utc, utcnow

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters

MIN_PASSWORD_LENGTH = 12
SESSION_COOKIE = "session"


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Used when the username does not exist, so a failed login takes the same time
# whether or not the account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def authenticate(db: Session, username: str, password: str) -> AdminUser | None:
    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    return user if verify_password(user.password_hash, password) else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: AdminUser, ttl_hours: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=_token_hash(token),
            user_id=user.id,
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
    )
    db.commit()
    return token


def user_for_token(db: Session, token: str | None) -> AdminUser | None:
    if not token:
        return None
    row = db.get(LoginSession, _token_hash(token))
    if row is None:
        return None
    if as_utc(row.expires_at) <= utcnow():
        db.delete(row)
        db.commit()
        return None
    return db.get(AdminUser, row.user_id)


def revoke_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
        db.commit()


def revoke_user_sessions(db: Session, user_id: str) -> None:
    """Sign a user out everywhere (used after a password reset)."""
    db.execute(delete(LoginSession).where(LoginSession.user_id == user_id))


def purge_expired_sessions(db: Session) -> int:
    result = db.execute(delete(LoginSession).where(LoginSession.expires_at <= utcnow()))
    db.commit()
    return result.rowcount or 0


class LoginThrottle:
    """Locks out a client address after repeated failed logins."""

    def __init__(self, max_failures: int, lockout_seconds: int) -> None:
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        window_start = now - self.lockout_seconds
        recent = [t for t in self._failures.get(key, []) if t > window_start]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)  # don't keep entries for every address ever seen
        return recent

    def is_locked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._failures[key] = self._recent(key, now) + [now]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
