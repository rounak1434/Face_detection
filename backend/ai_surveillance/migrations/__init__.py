"""Database migrations (Alembic), run automatically at startup.

Databases created before migrations existed (by SQLAlchemy create_all) have
the baseline tables but no alembic_version table. They are stamped as the
baseline revision, not altered, and then upgraded normally.
"""

import logging
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
BASELINE_TABLES = {"admin_users", "login_sessions", "captures", "devices"}


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


def upgrade_to_head(engine: Engine) -> str:
    cfg = alembic_config(engine)
    tables = set(inspect(engine).get_table_names())
    action = "upgraded"
    if "alembic_version" not in tables and tables & BASELINE_TABLES:
        missing = BASELINE_TABLES - tables
        if missing:
            # A partial pre-migration schema cannot be stamped safely.
            raise RuntimeError(f"Existing database is missing baseline tables {sorted(missing)}; restore a backup")
        command.stamp(cfg, BASELINE)
        action = "stamped legacy database as baseline, then upgraded"
        log.info("Existing database without migration history stamped as %s", BASELINE)
    before = current_revision(engine)
    command.upgrade(cfg, "head")
    after = current_revision(engine)
    if before == after and action == "upgraded":
        action = "already at head"
    log.info("Database schema at %s (%s)", after, action)
    return action
