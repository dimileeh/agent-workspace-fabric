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
from awf.service.workspaces import WorkspaceOwnedPathConflictError, WorkspaceService


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
    owned_paths: list[str] | None = None,
) -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": repo_url, "base_branch": base_branch},
        task={
            "title": title,
            "prompt": "Do policy-sensitive work.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "owned_paths": list(owned_paths or []),
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        resources={},
    )


@pytest.mark.unit
async def test_create_v2_acquires_conflict_lock_before_lookup(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    original_lock = WorkspaceRepository.acquire_owned_path_conflict_lock
    original_lookup = WorkspaceRepository.find_active_owned_path_conflicts

    async def lock_spy(
        self: WorkspaceRepository,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ) -> None:
        order.append("lock")
        await original_lock(
            self,
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
        )

    async def lookup_spy(
        self: WorkspaceRepository,
        *,
        repo_url: str,
        branch_base: str,
        owned_paths: list[str],
    ):
        order.append("lookup")
        return await original_lookup(
            self,
            repo_url=repo_url,
            branch_base=branch_base,
            owned_paths=owned_paths,
        )

    monkeypatch.setattr(WorkspaceRepository, "acquire_owned_path_conflict_lock", lock_spy)
    monkeypatch.setattr(WorkspaceRepository, "find_active_owned_path_conflicts", lookup_spy)

    service = WorkspaceService(factory)

    await service.create_v2(_request(title="new", owned_paths=["src/awf/api/**"]))

    assert order[:2] == ["lock", "lookup"]


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
async def test_create_v2_rejects_conflicts_with_useful_detail(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    existing = await service.create_v2(
        _request(title="existing", owned_paths=["src/awf/service/**"])
    )

    with pytest.raises(WorkspaceOwnedPathConflictError) as exc_info:
        await service.create_v2(
            _request(title="new", owned_paths=["src/awf/service/workspaces.py"])
        )

    assert exc_info.value.error_code == "WORKSPACE_OWNED_PATH_CONFLICT"
    assert exc_info.value.message == "Requested owned paths overlap an active workspace."
    assert exc_info.value.detail == {
        "workspace_ids": [existing.id],
        "conflicts": [
            {
                "workspace_id": existing.id,
                "existing_path": "src/awf/service/**",
                "requested_path": "src/awf/service/workspaces.py",
            }
        ],
    }
