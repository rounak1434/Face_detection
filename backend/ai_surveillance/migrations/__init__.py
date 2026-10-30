"""Database migrations (Alembic), run automatically at api startup.

Three cases:
- empty database: created from the migrations;
- database already under Alembic: upgraded to head (no-op if current);
- database created before migrations existed (by SQLAlchemy create_all):
  every baseline table and column is compared with the baseline revision
  (names, type family, nullability, primary keys, unique columns, foreign
  key). Only a match is stamped as 0001_baseline; anything else stops
  startup with a description of the difference. Nothing is altered or
  dropped in that case.

Before any schema change to an existing SQLite file, a consistent copy is
written next to it (<db>.pre-<revision>-<UTC time>.bak).
"""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
BASELINE = "0001_baseline"

# The schema 0001_baseline creates, i.e. what the pre-migration create_all
# produced (checked against a real database file). Per column:
# (type family, nullable, primary key). Type families rather than exact
# types, because SQLite does not enforce lengths: VARCHAR(32) and
# VARCHAR(64) are the same thing to it, but VARCHAR vs INTEGER is not.
TEXT, INT, DATETIME = "text", "integer", "datetime"
LEGACY_SCHEMA: dict[str, dict[str, tuple[str, bool, bool]]] = {
    "admin_users": {
        "id": (TEXT, False, True), "username": (TEXT, False, False),
        "password_hash": (TEXT, False, False), "created_at": (DATETIME, False, False),
    },
    "login_sessions": {
        "token_hash": (TEXT, False, True), "user_id": (TEXT, False, False), "expires_at": (DATETIME, False, False),
    },
    "captures": {
        "id": (TEXT, False, True), "camera": (TEXT, False, False), "kind": (TEXT, False, False),
        "status": (TEXT, False, False), "created_at": (DATETIME, False, False), "file_name": (TEXT, True, False),
        "size_bytes": (INT, True, False), "duration_seconds": (INT, True, False),
        "frigate_export_id": (TEXT, True, False), "error": (TEXT, True, False),
    },
    "devices": {
        "id": (TEXT, False, True), "name": (TEXT, False, False), "kind": (TEXT, False, False),
        "host": (TEXT, False, False), "rtsp_port": (INT, False, False), "username": (TEXT, True, False),
        "password_enc": (TEXT, True, False), "main_path": (TEXT, False, False), "sub_path": (TEXT, True, False),
        "status": (TEXT, False, False), "status_detail": (TEXT, True, False), "test_result": (TEXT, True, False),
        "tested_at": (DATETIME, True, False), "created_at": (DATETIME, False, False),
        "updated_at": (DATETIME, False, False),
    },
}
LEGACY_UNIQUE = {("admin_users", "username"), ("devices", "name")}
LEGACY_FOREIGN_KEYS = {("login_sessions", "user_id", "admin_users", "id")}

BASELINE_SCHEMA: dict[str, set[str]] = {table: set(cols) for table, cols in LEGACY_SCHEMA.items()}


def type_family(sql_type: str) -> str:
    """Map a declared SQLite column type to a family (SQLite type-affinity rules, plus DATETIME)."""
    t = sql_type.upper()
    if "DATETIME" in t or "TIMESTAMP" in t:
        return DATETIME
    if "INT" in t:
        return INT
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return TEXT
    return f"other({t.split('(')[0] or 'none'})"


class SchemaMismatch(RuntimeError):
    """An existing pre-migration database does not match the baseline exactly."""


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    cfg.attributes["engine"] = engine
    return cfg


def head_revision(engine: Engine) -> str:
    return ScriptDirectory.from_config(alembic_config(engine)).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def legacy_schema_differences(engine: Engine) -> list[str]:
    """Differences between an unversioned database and the baseline; empty if identical.

    Compares columns (names, type family, nullability, primary key), the two
    unique columns and the one foreign key. Messages contain only table and
    column names and types, never data.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = []
    for table, expected in LEGACY_SCHEMA.items():
        if table not in tables:
            problems.append(f"table {table!r} is missing")
            continue
        actual = {c["name"]: c for c in insp.get_columns(table)}
        pk = set(insp.get_pk_constraint(table).get("constrained_columns") or [])
        if missing := expected.keys() - actual.keys():
            problems.append(f"{table}: missing columns {sorted(missing)}")
        if extra := actual.keys() - expected.keys():
            problems.append(f"{table}: unexpected columns {sorted(extra)}")
        for name in sorted(expected.keys() & actual.keys()):
            family, nullable, is_pk = expected[name]
            col = actual[name]
            got_family = type_family(str(col["type"]))
            if got_family != family:
                problems.append(f"{table}.{name}: type {col['type']} ({got_family}), expected {family}")
            if is_pk != (name in pk):
                problems.append(f"{table}.{name}: primary key is {name in pk}, expected {is_pk}")
            elif not is_pk and bool(col["nullable"]) != nullable:
                # SQLite reports PK columns as nullable unless declared NOT NULL; PK columns are checked above.
                problems.append(f"{table}.{name}: nullable is {bool(col['nullable'])}, expected {nullable}")
    for table, column in sorted(LEGACY_UNIQUE):
        if table in tables and column in {c["name"] for c in insp.get_columns(table)}:
            unique = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
            unique |= {tuple(i["column_names"]) for i in insp.get_indexes(table) if i.get("unique")}
            if (column,) not in unique:
                problems.append(f"{table}.{column}: expected a UNIQUE constraint")
    for table, column, ref_table, ref_column in sorted(LEGACY_FOREIGN_KEYS):
        if table in tables:
            fks = {(tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                   for fk in insp.get_foreign_keys(table)}
            if ((column,), ref_table, (ref_column,)) not in fks:
                problems.append(f"{table}.{column}: expected a foreign key to {ref_table}.{ref_column}")
    return problems


def status(engine: Engine) -> dict:
    """Read-only summary used by `ai-surveillance db status`."""
    tables = set(inspect(engine).get_table_names())
    info = {"head": head_revision(engine), "current": None, "state": "empty"}
    if "alembic_version" in tables:
        info["current"] = current_revision(engine)
        info["state"] = "at head" if info["current"] == info["head"] else "upgrade needed"
    elif tables & BASELINE_SCHEMA.keys():
        diffs = legacy_schema_differences(engine)
        info["state"] = "legacy, matches baseline" if not diffs else "legacy, DOES NOT match baseline"
        info["differences"] = diffs
    return info


def sqlite_path(engine: Engine) -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    db = engine.url.database
    return Path(db) if db and db != ":memory:" else None


def backup_sqlite(engine: Engine, label: str) -> Path | None:
    """Write a consistent copy of the SQLite file (online backup API, safe with WAL)."""
    path = sqlite_path(engine)
    if path is None or not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.pre-{label}-{stamp}.bak")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("Database backed up to %s before migration", target.name)
    return target


def upgrade_to_head(engine: Engine, backup: bool = True) -> str:
    cfg = alembic_config(engine)
    tables = set(inspect(engine).get_table_names())
    head = head_revision(engine)

    if not tables:
        command.upgrade(cfg, "head")
        log.info("New database created at %s", head)
        return "created"

    if "alembic_version" not in tables:
        if not tables & BASELINE_SCHEMA.keys():
            raise SchemaMismatch(
                f"Database has tables {sorted(tables)} but none of this application's; refusing to touch it")
        diffs = legacy_schema_differences(engine)
        if diffs:
            raise SchemaMismatch(
                "Existing database was created before migrations but does not match the baseline: "
                + "; ".join(diffs) + ". Nothing was changed. See docs/DATABASE.md.")
        if backup:
            backup_sqlite(engine, "stamp")
        command.stamp(cfg, BASELINE)
        log.info("Pre-migration database verified and stamped as %s", BASELINE)
        command.upgrade(cfg, "head")
        return "stamped legacy database as baseline, then upgraded"

    current = current_revision(engine)
    if current == head:
        log.info("Database schema at %s (already at head)", head)
        return "already at head"
    if backup:
        backup_sqlite(engine, current or "unversioned")
    command.upgrade(cfg, "head")
    log.info("Database upgraded from %s to %s", current, head)
    return "upgraded"
