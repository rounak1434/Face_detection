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
    legacy_schema_differences,
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


def _create_baseline(engine) -> None:
    """The pre-migration schema (what create_all wrote then; 0001_baseline reproduces it), unversioned.

    Later revisions change the models, so create_all no longer gives the legacy schema.
    """
    command.upgrade(alembic_config(engine), BASELINE)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))


def _insert_legacy_rows(engine) -> None:
    """One record per table, in baseline columns only (plain SQL: the models have newer columns)."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO admin_users (id, username, password_hash, created_at) "
                          "VALUES ('u1', 'admin', 'argon2-hash', '2026-10-01 00:00:00')"))
        conn.execute(text("INSERT INTO login_sessions (token_hash, user_id, expires_at) "
                          "VALUES (:token, 'u1', '2026-10-02 00:00:00')"), {"token": "a" * 64})
        conn.execute(text("INSERT INTO captures (id, camera, kind, status, created_at, file_name) "
                          "VALUES ('c1', 'entrance', 'snapshot', 'ready', '2026-10-01 00:00:00', :file)"),
                     {"file": "b" * 32 + ".jpg"})
        conn.execute(text("INSERT INTO devices (id, name, kind, host, rtsp_port, password_enc, main_path, status, "
                          "created_at, updated_at) VALUES (:id, 'Gate NVR', 'nvr', '192.168.1.20', 554, "
                          "'v1:ciphertext', '/live', 'connected', '2026-10-01 00:00:00', '2026-10-01 00:00:00')"),
                     {"id": "d" * 32})


def _legacy_with_data(db: Database) -> None:
    """A database as the pre-migration code created it, with records in every table."""
    _create_baseline(db.engine)
    _insert_legacy_rows(db.engine)


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


def test_baseline_schema_constant_matches_the_baseline_revision(db):
    _create_baseline(db.engine)
    insp = inspect(db.engine)
    for table, columns in BASELINE_SCHEMA.items():
        assert {c["name"] for c in insp.get_columns(table)} == columns


def test_upgrade_from_baseline_keeps_devices_and_leaves_them_out_of_frigate(db):
    _create_baseline(db.engine)
    _insert_legacy_rows(db.engine)
    command.stamp(alembic_config(db.engine), BASELINE)
    assert db.migrate() == "upgraded"
    with db.sessionmaker() as s:
        device = s.query(Device).one()
        assert (device.name, device.password_enc, device.status) == ("Gate NVR", "v1:ciphertext", "connected")
        assert device.use_in_frigate is False


def test_downgrade_to_baseline_keeps_device_rows(db):
    db.migrate()
    with db.sessionmaker() as s:
        s.add(Device(name="Gate NVR", kind="nvr", host="192.168.1.20", rtsp_port=554, main_path="/live",
                     password_enc="v1:ciphertext", use_in_frigate=True))
        s.commit()
    command.downgrade(alembic_config(db.engine), BASELINE)
    assert "use_in_frigate" not in {c["name"] for c in inspect(db.engine).get_columns("devices")}
    assert legacy_schema_differences(db.engine) == []
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT name, password_enc FROM devices")).all() == [("Gate NVR", "v1:ciphertext")]


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


def _legacy_ddl() -> dict[str, str]:
    """CREATE TABLE statements exactly as the pre-migration create_all wrote them."""
    import tempfile

    from sqlalchemy import create_engine

    with tempfile.TemporaryDirectory() as d:
        engine = create_engine(f"sqlite:///{d}/ddl.db")
        _create_baseline(engine)
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name, sql FROM sqlite_master WHERE type='table'")).all()
        engine.dispose()
    return dict(rows)


def _make_legacy(db_path, table, old, new):
    """A legacy database where one table's DDL has `old` replaced by `new`."""
    ddl = _legacy_ddl()
    assert old in ddl[table], f"{old!r} not in {table} DDL: {ddl[table]}"
    ddl[table] = ddl[table].replace(old, new, 1)
    con = sqlite3.connect(db_path)
    for name in ("admin_users", "login_sessions", "captures", "devices"):
        con.execute(ddl[name])
    con.commit()
    con.close()


def test_exact_legacy_ddl_matches(db_path):
    _make_legacy(db_path, "devices", "rtsp_port", "rtsp_port")  # unchanged
    database = Database(f"sqlite:///{db_path}")
    try:
        assert database.migrate().startswith("stamped legacy database")
    finally:
        database.engine.dispose()


def test_varchar_length_difference_is_tolerated(db_path):
    # SQLite does not enforce VARCHAR lengths; a different length is the same schema to it.
    _make_legacy(db_path, "captures", "camera VARCHAR(64)", "camera VARCHAR(80)")
    database = Database(f"sqlite:///{db_path}")
    try:
        assert database.migrate().startswith("stamped legacy database")
    finally:
        database.engine.dispose()


@pytest.mark.parametrize(
    "table, old, new, fragment",
    [
        ("devices", "rtsp_port INTEGER NOT NULL", "rtsp_port VARCHAR(10) NOT NULL",
         "devices.rtsp_port: type VARCHAR(10) (text), expected integer"),
        ("captures", "created_at DATETIME NOT NULL", "created_at INTEGER NOT NULL",
         "captures.created_at: type INTEGER (integer), expected datetime"),
        ("devices", "host VARCHAR(253) NOT NULL", "host VARCHAR(253)",
         "devices.host: nullable is True, expected False"),
        ("captures", "file_name VARCHAR(128)", "file_name VARCHAR(128) NOT NULL",
         "captures.file_name: nullable is False, expected True"),
        ("admin_users", "UNIQUE (username)", "CHECK (1)", "admin_users.username: expected a UNIQUE constraint"),
        ("login_sessions", "FOREIGN KEY(user_id) REFERENCES admin_users (id) ON DELETE CASCADE", "CHECK (1)",
         "login_sessions.user_id: expected a foreign key to admin_users.id"),
    ],
    ids=["type-int-as-text", "type-datetime-as-int", "nullable-lost", "nullable-added", "unique-missing", "fk-missing"],
)
def test_legacy_constraint_mismatch_is_refused_and_untouched(db_path, table, old, new, fragment):
    _make_legacy(db_path, table, old, new)
    database = Database(f"sqlite:///{db_path}")
    try:
        with pytest.raises(SchemaMismatch) as exc:
            database.migrate()
        assert fragment in str(exc.value)
        assert "alembic_version" not in inspect(database.engine).get_table_names()
        assert _backups(db_path) == []  # nothing was even backed up, let alone changed
        assert status(database.engine)["state"] == "legacy, DOES NOT match baseline"
    finally:
        database.engine.dispose()


def test_diagnostic_contains_no_data(db):
    _legacy_with_data(db)
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE devices ADD COLUMN surprise TEXT"))
    with pytest.raises(SchemaMismatch) as exc:
        db.migrate()
    message = str(exc.value)
    for secret in ("argon2-hash", "v1:ciphertext", "192.168.1.20", "Gate NVR"):
        assert secret not in message


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


def test_gallery_revision_downgrades_alone_and_keeps_face_events(db):
    db.migrate()
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO face_events (id, frigate_event_id, camera, started_at, ended_at, status, "
                          "face_count, created_at) VALUES ('e1', '1.0-abc', 'entrance', '2026-10-29 12:00:00', "
                          "'2026-10-29 12:00:05', 'no_faces_detected', 0, '2026-10-29 12:00:06')"))
        conn.execute(text("INSERT INTO people (id, display_name, consent_date, consent_note, created_by, "
                          "created_at, updated_at) VALUES ('p1', 'X', '2026-10-01', 'form', 'admin', "
                          "'2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
    assert inspect(db.engine).get_columns("people")[5]["name"] == "active"
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT active FROM people")).scalar() == 1  # server default: active
    command.downgrade(alembic_config(db.engine), "0003_face_events")
    tables = set(inspect(db.engine).get_table_names())
    assert not tables & {"people", "reference_faces", "audit_log"} and "face_events" in tables
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM face_events")).all() == [("e1",)]
    assert db.migrate() == "upgraded"


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
