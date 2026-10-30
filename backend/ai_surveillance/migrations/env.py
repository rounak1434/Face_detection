"""Alembic environment: uses the engine passed in by the application."""

from alembic import context

from ai_surveillance.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = config.attributes["engine"]
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode to alter tables
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) migrations are not supported")
run_migrations_online()
