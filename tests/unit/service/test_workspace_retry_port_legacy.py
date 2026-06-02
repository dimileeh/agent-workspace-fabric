"""Legacy host identity retry-port tests split from test_workspace_retry_port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings
from awf.db.repositories import ResourceReservationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import (
    WorkspaceRetrySourceRuntimeNotReleasedError,
    create_workspace_row,
    retry_workspace_row,
)
from tests.postgres import postgres_test_engine
from tests.unit.service.test_workspace_retry_port import (
    _mark_failed,
    _request_with_preflight_override,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def test_retry_blocks_unreleased_legacy_source_hostname_with_local_reservation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Legacy source hostnames still map to the local reservation host."""
    settings = Settings(
        _env_file=None,
        host_home=str(tmp_path / "home"),
        docker_host="",
    )
    req = _request_with_preflight_override()
    companion_req = {
        "name": "sidecar",
        "repo_url": "git@github.com:example/sidecar.git",
        "base_branch": "main",
        "ports": [[5432, 5434]],
    }
    payload = req.model_dump(mode="python")
    payload["companions"] = [companion_req]
    req_with_companion = WorkspaceCreateRequest.model_validate(payload)

    async with factory() as session:
        source = await create_workspace_row(
            session,
            req_with_companion,
            settings=settings,
            provider_environ={},
        )
        await session.commit()

    await _mark_failed(factory, source.id, release_runtime=False)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(source.id)
        assert ws is not None
        ws.node_id = "worker-host-1"
        await session.commit()

    async with factory() as session:
        reservations = await ResourceReservationRepository(session).list_for_workspace(
            source.id, limit=1
        )
        if reservations:
            reservations[0].node_id = "local"
            await session.commit()

    async with factory() as session:
        with pytest.raises(WorkspaceRetrySourceRuntimeNotReleasedError):
            await retry_workspace_row(
                session,
                source.id,
                settings=settings,
                provider_readiness_override=True,
                provider_readiness_override_reason="stamped node id vs reservation node id test",
                provider_environ={},
            )
