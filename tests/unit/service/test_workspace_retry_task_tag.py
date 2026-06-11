"""Task-tag preservation tests for workspace retry."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from awf.db.repositories import WorkspaceRepository
from awf.service.workspaces import WorkspaceService
from tests.postgres import create_postgres_test_engine

from .test_workspace_retry_task_kind import _mark_failed, _request

pytestmark = pytest.mark.unit


@pytest.mark.unit
async def test_retry_preserves_source_task_tag() -> None:
    """Retrying a tagged workspace must carry the task_tag onto the new attempt."""
    engine = await create_postgres_test_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = WorkspaceService(factory)

    try:
        request = _request()
        tagged = request.model_copy(
            update={"task": request.task.model_copy(update={"task_tag": "PROJ-42"})}
        )
        first = await service.create(tagged)
        assert first.task_tag == "PROJ-42"
        await _mark_failed(factory, first.id)

        retried = await service.retry_workspace(
            first.id,
            provider_readiness_override=True,
            provider_readiness_override_reason="retry task-tag test",
        )
        assert retried.new_workspace_id != first.id

        async with factory() as session:
            repo = WorkspaceRepository(session)
            new_workspace = await repo.get(retried.new_workspace_id)
            assert new_workspace is not None
            assert new_workspace.task_tag == "PROJ-42"
    finally:
        await engine.dispose()
