"""Alembic environment — async-aware.

This file is invoked by ``alembic upgrade``, ``alembic downgrade``, and
``alembic revision --autogenerate``. It reads the database URL from AWF's
``Settings`` (so ``AWF_DATABASE_URL`` is honoured), imports the ORM metadata,
and runs migrations through an async engine.

The standard Alembic scaffolding is sync-only; this variant uses SQLAlchemy's
``connection.run_sync`` to bridge.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import models so their tables are registered on Base.metadata for autogenerate.
import awf.db.models  # noqa: F401
from awf.common.config import get_settings
from awf.db.base import Base

config = context.config

# Populate sqlalchemy.url from AWF_DATABASE_URL so alembic doesn't need a hardcoded DSN.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without connecting — used for review/diffs."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Async-friendly online runner."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
