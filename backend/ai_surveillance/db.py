"""Database engine, session handling and table models."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on read; all values are written in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginSession(Base):
    __tablename__ = "login_sessions"

    # Only the SHA-256 of the session token is stored.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Capture(Base):
    """A snapshot or clip saved on operator request."""

    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    camera: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # "snapshot" | "clip"
    status: Mapped[str] = mapped_column(String(16))  # "pending" | "ready" | "failed"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frigate_export_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Device(Base):
    """A configured video source: real IP camera / NVR, or the synthetic test stream."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # "ip_camera" | "nvr" | "synthetic"
    host: Mapped[str] = mapped_column(String(253))
    rtsp_port: Mapped[int] = mapped_column(Integer, default=554)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # AES-GCM ciphertext (credentials.py); the plaintext is never stored or returned.
    password_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    main_path: Mapped[str] = mapped_column(String(512))
    sub_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="not_tested")
    status_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    test_result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON per stream
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    is_sqlite = database_url.startswith("sqlite:///")
    if is_sqlite:
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            # WAL lets readers work while one writer commits (api + worker later);
            # busy_timeout waits for a lock instead of failing immediately.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine = make_engine(database_url)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> str:
        """Bring the schema to the latest Alembic revision; returns what was done."""
        from .migrations import upgrade_to_head

        return upgrade_to_head(self.engine)

    def session(self) -> Iterator[Session]:
        with self.sessionmaker() as session:
            yield session
