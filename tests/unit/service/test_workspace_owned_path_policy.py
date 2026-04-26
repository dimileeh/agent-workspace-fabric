"""Workspace owned-path conflict policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.service.workspaces import WorkspaceService


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _request(
    *,
    repo_url: str = "git@github.com:example/service.git",
    base_branch: str = "development",
    title: str = "Owned path policy",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
) -> WorkspaceCreateV2Request:
    task = {
        "title": title,
        "prompt": "Do policy-sensitive work.",
        "agent": "codex",
        "kind": "feature_branch_pr",
        "owned_paths": list(owned_paths or []),
    }
    if task_class is not None:
        task["task_class"] = task_class
    return WorkspaceCreateV2Request(
        repo={"url": repo_url, "base_branch": base_branch},
        task=task,
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        resources={},
    )


@pytest.mark.unit
async def test_create_v2_allows_empty_requested_owned_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    await service.create_v2(_request(title="existing", owned_paths=["src/awf/api/**"]))

    created = await service.create_v2(_request(title="new", owned_paths=[]))

    assert created.owned_paths == []


@pytest.mark.unit
async def test_create_v2_allows_non_overlapping_owned_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    await service.create_v2(_request(title="existing", owned_paths=["src/awf/api/**"]))

    created = await service.create_v2(_request(title="new", owned_paths=["docs/**"]))

    assert created.owned_paths == ["docs/**"]


@pytest.mark.unit
async def test_create_v2_ignores_terminal_and_teardown_conflicts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create_v2(_request(title="existing", owned_paths=["src/awf/api/**"]))
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(existing.id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.completed.value
        await session.commit()

    created = await service.create_v2(
        _request(title="new", owned_paths=["src/awf/api/routes/workspaces.py"])
    )

    assert created.owned_paths == ["src/awf/api/routes/workspaces.py"]


@pytest.mark.unit
async def test_create_v2_allows_overlap_and_records_risk_event(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create_v2(
        _request(
            title="existing",
            task_class="refactor_task",
            owned_paths=["src/awf/service/**"],
        )
    )

    created = await service.create_v2(
        _request(
            title="new",
            task_class="docs_task",
            owned_paths=["src/awf/service/workspaces.py"],
        )
    )
    events = await service.list_events(
        created.id,
        event_type="workspace.owned_path_overlap_risk",
    )

    assert created.owned_paths == ["src/awf/service/workspaces.py"]
    assert events is not None
    assert len(events) == 1
    assert events[0].reason_code == "OWNED_PATH_OVERLAP_RISK"
    assert events[0].payload == {
        "warning_code": "OWNED_PATH_OVERLAP_RISK",
        "message": (
            "Owned paths overlap active workspaces; this may require rebase "
            "or conflict resolution."
        ),
        "workspace_ids": [existing.id],
        "overlaps": [
            {
                "workspace_id": existing.id,
                "existing_path": "src/awf/service/**",
                "requested_path": "src/awf/service/workspaces.py",
            }
        ],
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_class",
    ["refactor_task", "docs_task", "test_task"],
)
async def test_create_v2_overlap_is_advisory_for_refactor_docs_and_test_tasks(
    factory: async_sessionmaker[AsyncSession],
    task_class: str,
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create_v2(
        _request(
            title=f"existing {task_class}",
            task_class=task_class,
            owned_paths=["src/awf/api/**"],
        )
    )

    created = await service.create_v2(
        _request(
            title=f"new {task_class}",
            task_class=task_class,
            owned_paths=["src/awf/api/routes/workspaces.py"],
        )
    )
    events = await service.list_events(
        created.id,
        event_type="workspace.owned_path_overlap_risk",
    )

    assert created.owned_paths == ["src/awf/api/routes/workspaces.py"]
    assert events is not None
    assert events[0].payload is not None
    assert events[0].payload["workspace_ids"] == [existing.id]
