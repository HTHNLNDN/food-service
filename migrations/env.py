"""Alembic environment: hand-written SQL migrations against DATABASE_URL (no ORM models)."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

if context.config.config_file_name is not None:  # "Running upgrade ..." lines in container logs
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

# Arbitrary app-wide key: serializes concurrent `alembic upgrade` runs (e.g. a restart loop).
_MIGRATION_LOCK = 7300001


def _url() -> str:
    # An explicit URL (tests, evals) wins over the environment, so a DATABASE_URL loaded from a
    # developer's .env can never redirect a test migration at a real database.
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            # Held until this transaction commits; DDL is transactional, so a failure rolls back.
            connection.execute(text(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK})"))
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
