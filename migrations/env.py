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
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

# Import models so their tables are registered on Base.metadata for autogenerate.
import awf.db.models  # noqa: F401
from awf.common.config import get_settings
from awf.db.base import Base

config = context.config

# Populate sqlalchemy.url from AWF_DATABASE_URL so alembic doesn't need a hardcoded DSN.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url.replace("%", "%%"))

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
    raw_url = config.get_main_option("sqlalchemy.url")
    parsed_url = make_url(raw_url)
    query = dict(parsed_url.query)
    search_path = query.pop("awf_search_path", None)
    null_pool = query.pop("awf_null_pool", None)
    connect_timeout = query.pop("awf_connect_timeout", None)
    connect_attempts = query.pop("awf_connect_attempts", None)
    connect_retries = query.pop("awf_connect_retries", None)
    connect_args = {}
    if search_path is not None:
        if isinstance(search_path, tuple):
            search_path = search_path[0]
        connect_args["server_settings"] = {"search_path": str(search_path)}
    if connect_timeout is not None:
        if isinstance(connect_timeout, tuple):
            connect_timeout = connect_timeout[0]
        connect_args["timeout"] = float(connect_timeout)
    if (
        search_path is not None
        or null_pool is not None
        or connect_timeout is not None
        or connect_attempts is not None
        or connect_retries is not None
    ):
        parsed_url = parsed_url.set(query=query)

    connectable = create_async_engine(
        parsed_url.render_as_string(hide_password=False),
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
