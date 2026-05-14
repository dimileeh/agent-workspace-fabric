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
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf import __version__
from awf.api.deps import require_api_token
from awf.api.routes import (
    artifacts,
    callbacks,
    controls,
    events,
    health,
    locks,
    logs,
    merge_queue,
    metrics,
    operations,
    runtime,
    tasks,
    validation,
    workspaces,
    ws,
)
from awf.common.config import Settings, get_settings
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Real-deploy lifespan: read settings, build engine, configure DB, tear down on shutdown.

    Tests bypass this path by constructing the app without a lifespan and calling
    ``configure_database`` directly.
    """
    health.reset_egress_audit_summary_counts_task(app.state)
    settings: Settings = get_settings()
    engine = make_engine(settings.database_url)

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
    health.reset_egress_audit_summary_counts_task(app.state)

    app.include_router(health.router)
    app.include_router(callbacks.router)
    app.include_router(workspaces.router)
    app.include_router(workspaces.router_v2)
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
    app.include_router(ws.router)

    _install_openapi_auth_contract(app)

    return app


def _install_openapi_auth_contract(app: FastAPI) -> None:
    """Keep the OpenAPI auth contract aligned with AWF auth dependency semantics."""
    default_openapi = app.openapi
    auth_required_operations = _auth_required_operations(app)

    def openapi_with_auth_contract() -> dict[str, Any]:
        openapi_schema = default_openapi()
        _mark_authorization_header_parameters_required(
            openapi_schema,
            auth_required_operations,
        )
        return openapi_schema

    app.openapi = openapi_with_auth_contract  # type: ignore[method-assign]


def _auth_required_operations(app: FastAPI) -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not any(dependency.dependency is require_api_token for dependency in route.dependencies):
            continue
        for method in route.methods:
            operations.add((route.path_format, method.lower()))
    return operations


def _mark_authorization_header_parameters_required(
    openapi_schema: dict[str, Any],
    auth_required_operations: set[tuple[str, str]],
) -> None:
    paths = openapi_schema.get("paths")
    if not isinstance(paths, dict):
        return

    for path, path_item in paths.items():
        if not isinstance(path, str):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if (
                not isinstance(method, str)
                or (path, method.lower()) not in auth_required_operations
            ):
                continue
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if (
                    isinstance(parameter, dict)
                    and parameter.get("in") == "header"
                    and parameter.get("name") == "authorization"
                ):
                    parameter["required"] = True
                    parameter["schema"] = _authorization_header_schema(parameter)


def _authorization_header_schema(parameter: dict[str, Any]) -> dict[str, Any]:
    schema = parameter.get("schema")
    title = (
        schema.get("title")
        if isinstance(schema, dict) and isinstance(schema.get("title"), str)
        else "Authorization"
    )
    return {"minLength": 1, "title": title, "type": "string"}
