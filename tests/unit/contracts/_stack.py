"""Shared REST + MCP + CLI stack fixture for cross-client contract tests.

Mirrors the proven ``OperatorStack`` shape used by
``tests/unit/mcp/test_mcp_operator_surfaces.py`` so REST and MCP responses can be
compared given identical seeded state. Adds a CLI hook that reuses the same
``AsyncClient`` so the operator-visible behavior of all three surfaces stays
in one place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.session import make_session_factory
from awf.mcp.server import build_mcp_server
from awf.service.disk import DiskCheck
from awf.service.workspaces import WorkspaceService


@dataclass(frozen=True)
class ContractStack:
    client: AsyncClient
    mcp: Any
    factory: async_sessionmaker[AsyncSession]
    settings: Settings
    auth_headers: dict[str, str]
    api_token: str
    service: WorkspaceService


def _ok_disk_check(settings: Settings) -> DiskCheck:
    threshold = settings.min_free_disk_bytes
    free = threshold + 1
    return DiskCheck(
        path=settings.work_dir,
        checked_path=settings.work_dir,
        total_bytes=free,
        used_bytes=0,
        free_bytes=free,
        percent_free=100.0,
        threshold_bytes=threshold,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def contract_stack(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[ContractStack]:
    """Build a single REST+MCP+CLI stack bound to one Postgres schema.

    The same ``WorkspaceService`` backs both the REST app and the MCP server so
    every client targets the exact same underlying state.
    """
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    monkeypatch.setenv("AWF_MIN_FREE_DISK_BYTES", "700")
    monkeypatch.setenv("AWF_WORKER_MAX_CONCURRENT_PROVISIONS", "5")
    monkeypatch.setenv("AWF_WORKER_MAX_CONCURRENT_EXECUTIONS", "2")
    get_settings.cache_clear()

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        api_token="secret",
        work_dir=str(work_dir),
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_disk_check

    service = WorkspaceService(factory, settings=settings)
    mcp = build_mcp_server(service=service, settings=settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield ContractStack(
            client=client,
            mcp=mcp,
            factory=factory,
            settings=settings,
            auth_headers={"Authorization": "Bearer secret"},
            api_token="secret",
            service=service,
        )
