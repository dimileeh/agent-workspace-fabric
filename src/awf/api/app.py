"""FastAPI application factory.

A factory pattern (rather than a module-level ``app = FastAPI()``) makes the app
easy to reconfigure per-test and prevents import-time side effects. Each test
gets its own fresh app instance via the ``client`` fixture in tests/conftest.py.
"""

from __future__ import annotations

from fastapi import FastAPI

from awf import __version__
from awf.api.routes import health


def create_app() -> FastAPI:
    """Build and return a configured AWF FastAPI application.

    The returned app is self-contained: all route modules are registered, middleware
    is configured, and the OpenAPI spec is wired. External dependencies (database,
    Docker client, etc.) are resolved lazily via FastAPI dependency injection so this
    factory remains side-effect-free and cheap to call in tests.
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
    )

    app.include_router(health.router)

    return app
