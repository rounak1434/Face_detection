"""Alembic migrations, always on temporary databases.

Covers fresh databases, pre-migration (create_all) databases with data,
schema verification before stamping, automatic backups, drift between
models and migrations, downgrade, and SQLite pragmas.
"""

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from ai_surveillance.db import AdminUser, Base, Capture, Database, Device, LoginSession
from ai_surveillance.migrations import (
    BASELINE,
    BASELINE_SCHEMA,
    SchemaMismatch,
    alembic_config,
    current_revision,
    head_revision,
    status,
)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "app.db"


@pytest.fixture
def db(db_path):
    database = Database(f"sqlite:///{db_path}")
    yield database
    database.engine.dispose()


def _legacy_with_data(db: Database) -> None:
    """A database as the pre-migration code created it, with records in every table."""
    Base.metadata.create_all(db.engine)
    with db.sessionmaker() as s:
        user = AdminUser(username="admin", password_hash="argon2-hash")
        s.add(user)
        s.flush()
        s.add(LoginSession(token_hash="a" * 64, user_id=user.id, expires_at=user.created_at))
        s.add(Capture(camera="entrance", kind="snapshot", status="ready", file_name="b" * 32 + ".jpg"))
        s.add(Device(name="Gate NVR", kind="nvr", host="192.168.1.20", rtsp_port=554, main_path="/live",
                     password_enc="v1:ciphertext"))
        s.commit()


def _backups(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.pre-*.bak"))


def test_fresh_database_is_created_at_head(db, db_path):
    assert db.migrate() == "created"
    assert current_revision(db.engine) == head_revision(db.engine)
    assert set(BASELINE_SCHEMA) <= set(inspect(db.engine).get_table_names())
    assert _backups(db_path) == []  # nothing to back up


def test_second_run_is_a_no_op_without_backup(db, db_path):
    db.migrate()
    assert db.migrate() == "already at head"
    assert _backups(db_path) == []


def test_models_match_migrations(db):
    """Fails when a model changes without a migration (autogenerate finds a diff)."""
    db.migrate()
    with db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_baseline_schema_constant_matches_models():
    for table, columns in BASELINE_SCHEMA.items():
        assert {c.name for c in Base.metadata.tables[table].columns} == columns


def test_legacy_database_with_data_is_verified_backed_up_and_stamped(db, db_path):
    _legacy_with_data(db)
    action = db.migrate()
    assert action.startswith("stamped legacy database")
    assert current_revision(db.engine) == head_revision(db.engine)
    with db.sessionmaker() as s:
        assert s.query(AdminUser).one().username == "admin"
        assert s.query(LoginSession).count() == 1
        assert s.query(Capture).one().camera == "entrance"
        assert s.query(Device).one().password_enc == "v1:ciphertext"
    # A consistent pre-stamp copy exists and contains the same data, unversioned.
    (backup,) = _backups(db_path)
    raw = sqlite3.connect(backup)
    assert raw.execute("select username from admin_users").fetchall() == [("admin",)]
    assert raw.execute("select count(*) from sqlite_master where name='alembic_version'").fetchone() == (0,)
    raw.close()


@pytest.mark.parametrize(
    "ddl, fragment",
    [
        ("ALTER TABLE devices ADD COLUMN surprise TEXT", "devices: unexpected columns ['surprise']"),
        ("DROP TABLE captures", "table 'captures' is missing"),
    ],
)
def test_legacy_database_that_differs_is_refused_and_untouched(db, ddl, fragment):
    _legacy_with_data(db)
    with db.engine.begin() as conn:
        conn.execute(text(ddl))
    before = sorted(inspect(db.engine).get_table_names())
    with pytest.raises(SchemaMismatch, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        db.migrate()
    inspector = inspect(db.engine)
    assert sorted(inspector.get_table_names()) == before
    assert "alembic_version" not in inspector.get_table_names()


def test_legacy_column_missing_is_refused(db_path):
    # Simulate an older hand-made table lacking a column.
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE admin_users (id VARCHAR(32) PRIMARY KEY, username VARCHAR(64), created_at DATETIME);")
    con.close()
    database = Database(f"sqlite:///{db_path}")
    try:
        with pytest.raises(SchemaMismatch, match="admin_users: missing columns \\['password_hash'\\]"):
            database.migrate()
    finally:
        database.engine.dispose()


def test_unrelated_database_is_refused(db):
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE something_else (id INTEGER PRIMARY KEY)"))
    with pytest.raises(SchemaMismatch, match="none of this application's"):
        db.migrate()


def test_status_is_read_only(db, db_path):
    _legacy_with_data(db)
    info = status(db.engine)
    assert info["state"] == "legacy, matches baseline" and info["differences"] == []
    assert "alembic_version" not in inspect(db.engine).get_table_names()
    db.migrate()
    assert status(db.engine)["state"] == "at head"


def test_cli_db_commands(db_path, monkeypatch, capsys):
    from ai_surveillance import cli
    from ai_surveillance.config import Settings

    settings = Settings(database_url=f"sqlite:///{db_path}", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["db", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "empty"
    assert cli.main(["db", "upgrade"]) == 0
    assert "created" in capsys.readouterr().out
    assert cli.main(["db", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "at head"


def test_downgrade_and_upgrade_round_trip(db):
    db.migrate()
    cfg = alembic_config(db.engine)
    command.downgrade(cfg, "base")
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert current_revision(db.engine) == head_revision(db.engine)


def test_baseline_is_the_first_revision():
    assert BASELINE == "0001_baseline"


def test_sqlite_runs_in_wal_mode(db, db_path):
    db.migrate()
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    raw = sqlite3.connect(db_path)
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    raw.close()


def test_migration_files_are_packaged():
    """The wheel must ship the revision scripts (setuptools package-data)."""
    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    data = pyproject["tool"]["setuptools"]["package-data"]["ai_surveillance"]
    assert "migrations/versions/*.py" in data and "migrations/script.py.mako" in data
