"""FastAPI application factory.

A factory pattern (rather than a module-level ``app = FastAPI()``) makes the app
easy to reconfigure per-test and prevents import-time side effects. Each test
gets its own fresh app instance via the ``client`` fixture in tests/conftest.py.

Database wiring:
    - ``configure_database(app, factory)`` attaches a session factory to ``app.state``
      so dependencies in ``awf.api.deps`` can yield sessions per request.
    - For production, ``lifespan`` (wired below) creates the engine + factory from
      settings. For tests, the ``client`` fixture creates an in-memory SQLite engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf import __version__
from awf.api.routes import (
    artifacts,
    controls,
    events,
    health,
    logs,
    operations,
    runtime,
    tasks,
    workspaces,
    ws,
)
from awf.common.config import Settings, get_settings
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory


def configure_database(
    app: FastAPI,
    factory: async_sessionmaker[AsyncSession],
    *,
    engine: AsyncEngine | None = None,
    database_url: str | None = None,
) -> None:
    """Attach a session factory to ``app.state`` for dependency injection."""
    app.state.db_session_factory = factory
    if engine is not None:
        app.state.db_engine = engine
    if database_url is not None:
        app.state.database_url = database_url
        from awf.api.deps import _sqlite_file_path, _sqlite_identity

        db_path = _sqlite_file_path(database_url)
        app.state.db_sqlite_identity = _sqlite_identity(db_path) if db_path is not None else None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Real-deploy lifespan: read settings, build engine, configure DB, tear down on shutdown.

    Tests bypass this path by constructing the app without a lifespan and calling
    ``configure_database`` directly.
    """
    settings: Settings = get_settings()
    engine = make_engine(settings.database_url)

    # For local SQLite we create tables at startup so the first run works out of the box.
    # For Postgres in prod we rely on Alembic migrations (applied separately by `alembic upgrade`).
    if settings.database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    configure_database(app, factory, engine=engine, database_url=settings.database_url)

    try:
        yield
    finally:
        current_engine = getattr(app.state, "db_engine", engine)
        await current_engine.dispose()
        if current_engine is not engine:
            await engine.dispose()


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    """Build and return a configured AWF FastAPI application.

    ``use_lifespan`` defaults to True for production. Tests pass ``use_lifespan=False``
    and call ``configure_database`` themselves so each test gets its own isolated DB.
    """
    app = FastAPI(
        title="Aira Agent Workspace Fabric",
        description=(
            "Isolated Docker execution substrate for AI coding agents. Exposes a REST API "
            "and an MCP server over the same underlying control plane."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan if use_lifespan else None,
    )

    app.include_router(health.router)
    app.include_router(workspaces.router)
    app.include_router(workspaces.router_v2)
    app.include_router(events.router)
    app.include_router(tasks.router)
    app.include_router(runtime.router)
    app.include_router(artifacts.router)
    app.include_router(logs.router)
    app.include_router(operations.router)
    app.include_router(controls.router)
    app.include_router(ws.router)

    return app
