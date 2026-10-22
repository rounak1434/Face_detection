"""Database engine, session handling and table models."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


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


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite:///"):
        Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine = make_engine(database_url)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        # Milestone 1 schema is created directly; migrations (Alembic) arrive
        # with the first schema change in milestone 2.
        Base.metadata.create_all(self.engine)

    def session(self) -> Iterator[Session]:
        with self.sessionmaker() as session:
            yield session
