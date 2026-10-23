"""Alembic migrations: fresh databases, pre-migration databases, drift, downgrade."""

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from ai_surveillance.db import AdminUser, Base, Database, Device
from ai_surveillance.migrations import BASELINE, alembic_config, current_revision, head_revision


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'app.db'}")
    yield database
    database.engine.dispose()


def test_fresh_database_is_created_at_head(db):
    assert db.migrate() == "upgraded"
    assert current_revision(db.engine) == head_revision(db.engine)
    assert {"admin_users", "login_sessions", "captures", "devices"} <= set(inspect(db.engine).get_table_names())


def test_second_run_is_a_no_op(db):
    db.migrate()
    assert db.migrate() == "already at head"


def test_models_match_migrations(db):
    """Fails when a model changes without a migration (autogenerate finds a diff)."""
    db.migrate()
    with db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_legacy_create_all_database_is_stamped_not_altered(db):
    # A database made by the pre-migration code path, with data in it.
    Base.metadata.create_all(db.engine)
    with db.sessionmaker() as s:
        s.add(AdminUser(username="admin", password_hash="x"))
        s.add(Device(name="Cam", kind="nvr", host="192.168.1.2", rtsp_port=554, main_path="/x"))
        s.commit()
    action = db.migrate()
    assert action.startswith("stamped legacy database")
    assert current_revision(db.engine) == head_revision(db.engine)
    with db.sessionmaker() as s:
        assert s.query(AdminUser).one().username == "admin"
        assert s.query(Device).one().name == "Cam"


def test_partial_legacy_schema_is_refused(db):
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE admin_users (id VARCHAR(32) PRIMARY KEY)"))
    with pytest.raises(RuntimeError, match="missing baseline tables"):
        db.migrate()


def test_downgrade_and_upgrade_round_trip(db):
    db.migrate()
    cfg = alembic_config(db.engine)
    command.downgrade(cfg, "base")
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert current_revision(db.engine) == head_revision(db.engine)


def test_baseline_is_the_first_revision(db):
    assert BASELINE == "0001_baseline"


def test_sqlite_runs_in_wal_mode(db, tmp_path):
    db.migrate()
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    # The setting persists in the file for other processes too.
    raw = sqlite3.connect(tmp_path / "app.db")
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    raw.close()


def test_migration_files_are_packaged():
    """The wheel must ship the revision scripts (setuptools package-data)."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    data = pyproject["tool"]["setuptools"]["package-data"]["ai_surveillance"]
    assert "migrations/versions/*.py" in data and "migrations/script.py.mako" in data
