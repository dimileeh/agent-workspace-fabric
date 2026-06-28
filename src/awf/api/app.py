"""FastAPI application factory.

A factory pattern (rather than a module-level ``app = FastAPI()``) makes the app
easy to reconfigure per-test and prevents import-time side effects. Each test
gets its own fresh app instance via the ``client`` fixture in tests/conftest.py.

Database wiring:
    - ``configure_database(app, factory)`` attaches a session factory to ``app.state``
      so dependencies in ``awf.api.deps`` can yield sessions per request.
    - For production, ``lifespan`` (wired below) creates the engine + factory from
      settings. Tests inject a PostgreSQL-backed factory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf import __version__
from awf.api.deps import (
    WebSocketAuthorizationDenialError,
    websocket_authorization_denial_handler,
)
from awf.api.routes import (
    artifacts,
    callbacks,
    controls,
    discovery,
    events,
    health,
    locks,
    logs,
    merge_queue,
    metrics,
    operations,
    runtime,
    service,
    tasks,
    validation,
    workspaces,
    ws,
)
from awf.common.config import Settings, get_settings, validate_production_settings
from awf.db.session import make_engine, make_session_factory
from awf.service.callbacks import shutdown_callback_target_validation_executor


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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Real-deploy lifespan: read settings, build engine, configure DB, tear down on shutdown.

    Tests bypass this path by constructing the app without a lifespan and calling
    ``configure_database`` directly.
    """
    health.reset_egress_audit_summary_counts_task(app.state)
    settings: Settings = get_settings()
    validate_production_settings(settings)
    engine = make_engine(settings.database_url)

    factory = make_session_factory(engine)
    configure_database(app, factory, engine=engine, database_url=settings.database_url)

    try:
        yield
    finally:
        try:
            current_engine = getattr(app.state, "db_engine", engine)
            await current_engine.dispose()
            if current_engine is not engine:
                await engine.dispose()
        finally:
            shutdown_callback_target_validation_executor(wait=False)


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    """Build and return a configured AWF FastAPI application.

    ``use_lifespan`` defaults to True for production. Tests pass ``use_lifespan=False``
    and call ``configure_database`` themselves so each test gets its own isolated DB.
    """
    app = FastAPI(
        title="Agent Workspace Fabric",
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
    health.reset_egress_audit_summary_counts_task(app.state)
    app.add_exception_handler(
        WebSocketAuthorizationDenialError,
        websocket_authorization_denial_handler,
    )

    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(callbacks.router)
    app.include_router(workspaces.router)
    app.include_router(events.router)
    app.include_router(tasks.router)
    app.include_router(locks.router)
    app.include_router(runtime.router)
    app.include_router(artifacts.router)
    app.include_router(logs.router)
    app.include_router(validation.router)
    app.include_router(merge_queue.router)
    app.include_router(metrics.router)
    app.include_router(operations.router)
    app.include_router(controls.router)
    app.include_router(service.router)
    app.include_router(ws.router)

    return app
