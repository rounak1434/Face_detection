"""Database engine, session handling and table models."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event, false
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
    # Operator approval for "Apply to Frigate" (ADR-0003); applied only while status is "connected".
    use_in_frigate: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class FaceEvent(Base):
    """One Frigate person event, analysed once for faces (ADR-0004). Detection only: no identity."""

    __tablename__ = "face_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    frigate_event_id: Mapped[str] = mapped_column(String(30), unique=True)  # one row per Frigate event
    camera: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # faces_detected | no_faces_detected | frame_unavailable | frame_rejected | inference_failed | gave_up
    status: Mapped[str] = mapped_column(String(32), index=True)
    face_count: Mapped[int] = mapped_column(Integer, default=0)
    frame_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # recording | snapshot
    frame_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    frame_file: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frame_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detector: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "yunet 2023mar"
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FaceDetection(Base):
    """A face found in a face event's frame: where, how confident, how usable. Not who."""

    __tablename__ = "face_detections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("face_events.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # order by detection score within the event
    box_x: Mapped[int] = mapped_column(Integer)
    box_y: Mapped[int] = mapped_column(Integer)
    box_w: Mapped[int] = mapped_column(Integer)
    box_h: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    keypoints: Mapped[str] = mapped_column(Text)  # JSON, 5 [x, y] pairs
    quality: Mapped[str] = mapped_column(Text)  # JSON: face_px, small_face, touches_edge, sharpness
    crop_file: Mapped[str | None] = mapped_column(String(64), nullable=True)


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

    def migrate(self, backup: bool = True) -> str:
        """Bring the schema to the latest Alembic revision; returns what was done.

        Raises migrations.SchemaMismatch, without changing anything, for a
        pre-migration database that does not match the baseline exactly.
        """
        from .migrations import upgrade_to_head

        return upgrade_to_head(self.engine, backup=backup)

    def session(self) -> Iterator[Session]:
        with self.sessionmaker() as session:
            yield session
