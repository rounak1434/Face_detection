"""Database migrations (Alembic), run automatically at api startup.

Three cases:
- empty database: created from the migrations;
- database already under Alembic: upgraded to head (no-op if current);
- database created before migrations existed (by SQLAlchemy create_all):
  every baseline table and column is compared with the baseline revision.
  Only an exact match is stamped as 0001_baseline; anything else stops
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

# Exactly the tables and columns that 0001_baseline creates, i.e. what the
# pre-migration create_all produced (checked against a real database file).
BASELINE_SCHEMA: dict[str, set[str]] = {
    "admin_users": {"id", "username", "password_hash", "created_at"},
    "login_sessions": {"token_hash", "user_id", "expires_at"},
    "captures": {"id", "camera", "kind", "status", "created_at", "file_name", "size_bytes",
                 "duration_seconds", "frigate_export_id", "error"},
    "devices": {"id", "name", "kind", "host", "rtsp_port", "username", "password_enc", "main_path",
                "sub_path", "status", "status_detail", "test_result", "tested_at", "created_at", "updated_at"},
}


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
    """Differences between an unversioned database and the baseline; empty if identical."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = []
    for table, expected in BASELINE_SCHEMA.items():
        if table not in tables:
            problems.append(f"table {table!r} is missing")
            continue
        actual = {c["name"] for c in insp.get_columns(table)}
        if missing := expected - actual:
            problems.append(f"{table}: missing columns {sorted(missing)}")
        if extra := actual - expected:
            problems.append(f"{table}: unexpected columns {sorted(extra)}")
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
