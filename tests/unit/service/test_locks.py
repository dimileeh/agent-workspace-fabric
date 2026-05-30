"""Read-only owned-path reservation service tests."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    repo_url: str = "git@github.com:example/app.git",
    branch_base: str = "main",
    task_class: str | None = "refactor_task",
    owned_paths: list[str] | None = None,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    created_at: datetime,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=title,
            task_prompt="Expose owned-path reservations to operators.",
            task_class=task_class,
            owned_paths=list(owned_paths or []),
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.pr_url = f"https://github.com/example/app/pull/{title[-1]}"
        workspace.created_at = created_at
        workspace.updated_at = created_at + timedelta(minutes=5)
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_list_workspace_locks_defaults_to_active_non_terminal_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import list_workspace_locks

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older_active_id = await _workspace(
        session_factory,
        title="Active 1",
        owned_paths=["src/awf/api/**"],
        status=WorkspaceStatus.requested,
        created_at=now,
    )
    newer_active_id = await _workspace(
        session_factory,
        title="Active 2",
        owned_paths=["src/awf/service/**"],
        status=WorkspaceStatus.monitoring_pr,
        created_at=now + timedelta(minutes=1),
    )
    completed_id = await _workspace(
        session_factory,
        title="Done 3",
        owned_paths=["docs/**"],
        status=WorkspaceStatus.completed,
        created_at=now + timedelta(minutes=2),
    )
    destroying_id = await _workspace(
        session_factory,
        title="Cleanup 4",
        owned_paths=["tests/**"],
        status=WorkspaceStatus.destroying,
        created_at=now + timedelta(minutes=3),
    )

    locks = await list_workspace_locks(session_factory)

    assert [lock.workspace_id for lock in locks] == [newer_active_id, older_active_id]
    assert completed_id not in {lock.workspace_id for lock in locks}
    assert destroying_id not in {lock.workspace_id for lock in locks}
    assert locks[0].title == "Active 2"
    assert locks[0].agent == "codex"
    assert locks[0].status == WorkspaceStatus.monitoring_pr.value
    assert locks[0].repo_url == "git@github.com:example/app.git"
    assert locks[0].branch_base == "main"
    assert locks[0].task_class == "refactor_task"
    assert locks[0].owned_paths == ("src/awf/service/**",)
    assert isinstance(hash(locks[0]), int)
    assert locks[0].pr_url == "https://github.com/example/app/pull/2"
    assert locks[0].created_at == now + timedelta(minutes=1)
    assert locks[0].updated_at == now + timedelta(minutes=6)


@pytest.mark.unit
async def test_list_workspace_locks_for_session_returns_page_items(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import list_workspace_locks_for_session

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        title="Session 1",
        owned_paths=["src/awf/service/locks.py"],
        status=WorkspaceStatus.requested,
        created_at=now,
    )

    async with session_factory() as session:
        locks = await list_workspace_locks_for_session(session)

    assert [lock.workspace_id for lock in locks] == [workspace_id]


@pytest.mark.unit
async def test_list_workspace_locks_for_session_uses_same_page_projection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import list_workspace_locks_for_session

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        title="Session 1",
        owned_paths=["src/awf/profiles/**"],
        status=WorkspaceStatus.ready,
        created_at=now,
    )

    async with session_factory() as session:
        locks = await list_workspace_locks_for_session(
            session,
            status=WorkspaceStatus.ready,
            limit=1,
        )

    assert [lock.workspace_id for lock in locks] == [workspace_id]
    assert locks[0].owned_paths == ("src/awf/profiles/**",)


@pytest.mark.unit
async def test_list_workspace_locks_applies_repo_task_class_status_and_limit_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import list_workspace_locks

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    matching_id = await _workspace(
        session_factory,
        title="Match 1",
        repo_url="git@github.com:example/app.git",
        task_class="test_task",
        owned_paths=["tests/unit/**"],
        status=WorkspaceStatus.ready,
        created_at=now,
    )
    await _workspace(
        session_factory,
        title="Wrong repo 2",
        repo_url="git@github.com:example/docs.git",
        task_class="test_task",
        owned_paths=["tests/unit/**"],
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=1),
    )
    await _workspace(
        session_factory,
        title="Wrong class 3",
        repo_url="git@github.com:example/app.git",
        task_class="docs_task",
        owned_paths=["docs/**"],
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=2),
    )
    await _workspace(
        session_factory,
        title="Wrong status 4",
        repo_url="git@github.com:example/app.git",
        task_class="test_task",
        owned_paths=["src/**"],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=3),
    )

    locks = await list_workspace_locks(
        session_factory,
        repo_url="git@github.com:example/app.git",
        task_class="test_task",
        status=WorkspaceStatus.ready,
        limit=1,
    )

    assert [lock.workspace_id for lock in locks] == [matching_id]


@pytest.mark.unit
async def test_list_workspace_lock_page_reports_more_rows_and_uses_next_cursor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import list_workspace_lock_page

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    oldest_id = await _workspace(
        session_factory,
        title="Old 1",
        status=WorkspaceStatus.ready,
        created_at=now,
    )
    middle_id = await _workspace(
        session_factory,
        title="Middle 2",
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=1),
    )
    newest_id = await _workspace(
        session_factory,
        title="Newest 3",
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=2),
    )

    first_page = await list_workspace_lock_page(
        session_factory,
        status=WorkspaceStatus.ready,
        limit=2,
    )

    assert [lock.workspace_id for lock in first_page.items] == [newest_id, middle_id]
    assert first_page.has_more is True
    assert first_page.next_cursor is not None

    second_page = await list_workspace_lock_page(
        session_factory,
        status=WorkspaceStatus.ready,
        limit=2,
        cursor=first_page.next_cursor,
    )

    assert [lock.workspace_id for lock in second_page.items] == [oldest_id]
    assert second_page.has_more is False
    assert second_page.next_cursor is None


@pytest.mark.unit
async def test_list_workspace_locks_includes_overlap_risk_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.locks import WorkspaceLockOverlapRisk, list_workspace_locks

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    existing_id = await _workspace(
        session_factory,
        title="Existing 1",
        task_class="refactor_task",
        owned_paths=["src/awf/service/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    overlapping_id = await _workspace(
        session_factory,
        title="Overlap 2",
        task_class="docs_task",
        owned_paths=["src/awf/service/workspaces.py"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    locks = await list_workspace_locks(session_factory)
    by_id = {lock.workspace_id: lock for lock in locks}

    assert by_id[overlapping_id].overlap_risks == (
        WorkspaceLockOverlapRisk(
            overlapping_workspace_id=existing_id,
            overlapping_owned_path="src/awf/service/**",
            owned_path="src/awf/service/workspaces.py",
        ),
    )
    assert by_id[existing_id].overlap_risks == (
        WorkspaceLockOverlapRisk(
            overlapping_workspace_id=overlapping_id,
            overlapping_owned_path="src/awf/service/workspaces.py",
            owned_path="src/awf/service/**",
        ),
    )


@pytest.mark.unit
async def test_list_workspace_locks_offloads_overlap_risk_calculation(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import locks as locks_service

    offloaded = False

    async def fake_to_thread(function, /, *args, **kwargs):
        nonlocal offloaded
        offloaded = True
        return function(*args, **kwargs)

    monkeypatch.setattr(locks_service.asyncio, "to_thread", fake_to_thread)

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _workspace(
        session_factory,
        title="Existing 1",
        owned_paths=["src/awf/service/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    await _workspace(
        session_factory,
        title="Overlap 2",
        owned_paths=["src/awf/service/locks.py"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    locks = await locks_service.list_workspace_locks(session_factory)

    assert offloaded is True
    assert any(lock.overlap_risks for lock in locks)


@pytest.mark.unit
async def test_lock_helpers_short_circuit_empty_overlap_inputs() -> None:
    from awf.service.locks import (
        _active_overlap_candidates,
        _workspace_overlap_risks_for_page,
    )

    class _SessionThatShouldNotExecute:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("empty lock inputs should not hit the database")

    assert await _active_overlap_candidates(_SessionThatShouldNotExecute(), []) == []  # type: ignore[arg-type]
    assert await _workspace_overlap_risks_for_page([], overlap_candidates=[]) == {}


@pytest.mark.unit
def test_overlap_risks_prefilters_candidate_owned_paths_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.common.owned_paths import interworkspace_owned_paths as real_interworkspace_owned_paths
    from awf.service import locks as locks_service

    calls_by_paths: dict[tuple[str, ...], int] = {}

    def counting_interworkspace_owned_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
        calls_by_paths[paths] = calls_by_paths.get(paths, 0) + 1
        return real_interworkspace_owned_paths(paths)

    monkeypatch.setattr(
        locks_service,
        "interworkspace_owned_paths",
        counting_interworkspace_owned_paths,
    )

    risks_by_workspace = locks_service._workspace_overlap_risks_by_id(
        (
            locks_service._OverlapWorkspace(
                workspace_id="page-src",
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                owned_paths=("src/awf/service/locks.py",),
            ),
            locks_service._OverlapWorkspace(
                workspace_id="page-docs",
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                owned_paths=("docs/usage.md",),
            ),
        ),
        (
            locks_service._OverlapWorkspace(
                workspace_id="candidate-src",
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                owned_paths=("src/awf/service/**",),
            ),
            locks_service._OverlapWorkspace(
                workspace_id="candidate-docs",
                repo_url="git@github.com:example/app.git",
                branch_base="main",
                owned_paths=("docs/**",),
            ),
        ),
    )

    assert set(risks_by_workspace) == {"page-src", "page-docs"}
    assert calls_by_paths[("src/awf/service/**",)] == 1
    assert calls_by_paths[("docs/**",)] == 1
    assert calls_by_paths[("src/awf/service/locks.py",)] == 1
    assert calls_by_paths[("docs/usage.md",)] == 1


@pytest.mark.unit
def test_lock_cursor_rejects_empty_workspace_id() -> None:
    from awf.service.locks import InvalidWorkspaceLockCursorError, _decode_cursor

    payload = {"created_at": datetime(2026, 4, 27, tzinfo=UTC).isoformat(), "workspace_id": ""}
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    with pytest.raises(InvalidWorkspaceLockCursorError, match="Invalid workspace lock cursor"):
        _decode_cursor(cursor)
