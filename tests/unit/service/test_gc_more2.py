from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.models import ResourceReservation, Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.gc import (
    FAILED_WORKSPACE_NO_WORK,
    TERMINAL_WORKSPACE_RETENTION_EXPIRED,
    WorkspaceGCCandidate,
    WorkspaceGCPath,
    WorkspaceGCPreserved,
    WorkspaceGCWorktreeRemoveResult,
    _classify_workspace_for_gc,
    _default_worktree_remover,
    _pr_has_merged,
    _release_gc_reservations,
    _run_worktree_remove,
    plan_terminal_workspace_gc,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)


class _StaticRuntimeInspector:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshot = snapshot

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        assert compose_project_name is not None
        return self.snapshot


@pytest.fixture(autouse=True)
def _mock_default_worktree_remover():
    with patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    ):
        yield


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    title: str = "gc candidate",
    compose_file_path: str | None = None,
    pr: bool = False,
    pr_merge_sha: str | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.compose_file_path = compose_file_path
        if pr:
            workspace.pr_url = "https://github.com/example/repo/pull/123"
            workspace.pr_number = 123
            workspace.pr_merge_sha = pr_merge_sha
        await session.commit()
        return workspace.id


async def _task_attempt_for_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> str:
    workspace = await session.get(Workspace, workspace_id)
    assert workspace is not None
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=f"gc-{workspace_id}",
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    return attempt.id


def test_classify_workspace_failed_has_work_but_expired_no_default_policy():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=False,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)


def test_pr_has_merged_true_when_merge_sha_set():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        pr_merge_sha="a" * 40,
    )
    assert _pr_has_merged(ws) is True


def test_pr_has_merged_false_when_merge_sha_none():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        pr_url="http://github.com/pr/1",
        pr_merge_sha=None,
    )
    assert _pr_has_merged(ws) is False


def test_pr_has_merged_false_when_no_pr_at_all():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
    )
    assert _pr_has_merged(ws) is False


async def test_gc_execute_calls_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="c" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        workspace_id_arg = getattr(candidate, "workspace_id", None)
        remover_calls.append(workspace_id_arg)
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.status == "succeeded"
    assert remover_calls == [workspace_id]
    assert result.worktree_removes[workspace_id].status == "succeeded"
    assert result.worktree_removes[workspace_id].reason_code == "WORKTREE_REMOVE_SUCCEEDED"


async def test_gc_execute_releases_resource_reservations(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="d" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        attempt_id = await _task_attempt_for_workspace(session, workspace_id)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=1024,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.status == "succeeded"
    assert workspace_id in result.reservation_releases
    assert result.reservation_releases[workspace_id]["released_count"] >= 1

    async with session_factory() as session:
        stmt = select(ResourceReservation).where(ResourceReservation.workspace_id == workspace_id)
        rows = list((await session.execute(stmt)).scalars())
        assert all(r.released_at is not None for r in rows)


async def test_gc_dry_run_does_not_remove_worktree_or_release_reservations(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="e" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        remover_calls.append("called")
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        attempt_id = await _task_attempt_for_workspace(session, workspace_id)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=None,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=False,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.dry_run is True
    assert remover_calls == []
    assert result.worktree_removes == {}
    assert result.reservation_releases == {}
    assert worktree.exists()

    async with session_factory() as session:
        stmt = select(ResourceReservation).where(
            ResourceReservation.workspace_id == workspace_id,
            ResourceReservation.released_at.is_(None),
        )
        active = list((await session.execute(stmt)).scalars())
        assert len(active) == 1


async def test_gc_partial_worktree_remove_failure_still_deletes_other_paths(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="f" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    async def _failing_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
            error="mirror not accessible",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_failing_worktree_remover,
    )

    assert result.status == "partial"
    assert result.worktree_removes[workspace_id].status == "failed"
    assert worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert workspace_id in result.reservation_releases


async def test_gc_reservation_release_failure_does_not_block_other_cleanup(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="g" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    async def _failing_release(
        self: object, workspace_id: str, **kwargs: object
    ) -> list[ResourceReservation]:
        raise RuntimeError("db connection lost")

    with patch.object(
        ResourceReservationRepository, "release_active_for_workspace", _failing_release
    ):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    assert result.status == "partial"
    assert result.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert workspace_id in result.reservation_releases
    assert result.reservation_releases[workspace_id].get("error") is not None


async def test_gc_idempotent_after_partial_failure(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="h" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    call_count = 0

    async def _partial_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return WorkspaceGCWorktreeRemoveResult(
                status="failed",
                reason_code="GIT_WORKTREE_REMOVE_FAILED",
                error="lock contention",
            )
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        attempt_id = await _task_attempt_for_workspace(session, workspace_id)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=None,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_partial_worktree_remover,
    )

    assert first.status == "partial"
    assert first.worktree_removes[workspace_id].status == "failed"

    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_partial_worktree_remover,
    )

    assert second.status == "succeeded"
    assert second.worktree_removes[workspace_id].status == "succeeded"
    assert second.reservation_releases[workspace_id]["released_count"] == 0


async def test_gc_result_dict_includes_worktree_removes_and_reservation_releases(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="i" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_worktree_remover,
    )

    payload = result.to_dict()
    assert "worktree_removes" in payload
    wr = payload["worktree_removes"][workspace_id]
    assert wr["status"] == "succeeded"
    assert wr["reason_code"] == "WORKTREE_REMOVE_SUCCEEDED"

    assert "reservation_releases" in payload
    assert isinstance(payload["reservation_releases"], dict)


async def test_plan_completed_pr_not_merged_in_preserved_examples(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha=None,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert plan.candidates == []
    preserved_ids = [p.workspace_id for p in plan.preserved]
    assert workspace_id in preserved_ids
    preserved = next(p for p in plan.preserved if p.workspace_id == workspace_id)
    assert preserved.reason_code == "COMPLETED_PR_NOT_MERGED"
    payload = plan.to_dict()
    preserved_payloads = [p for p in payload["preserved"] if p["workspace_id"] == workspace_id]
    assert preserved_payloads[0]["reason_code"] == "COMPLETED_PR_NOT_MERGED"


async def test_workspace_gc_plan_requires_pr_merge_property(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=datetime(2026, 4, 26, 12, tzinfo=UTC),
    )

    assert plan.requires_pr_merge is True
    payload = plan.to_dict()
    assert payload["policy"]["requires_pr_merge"] is True


async def test_failed_terminal_gc_plan_awaits_runtime_inspection_without_asyncio_run(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                compose_project_name="awf_failed_no_work",
                updated_at=now - timedelta(hours=200),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    snapshot = RuntimeSnapshot(
        stack_state="running",
        services=[
            RuntimeService(
                name="agent",
                state="running",
                command="sleep infinity",
                container_id="abc",
                image="awf-agent",
            )
        ],
    )
    monkeypatch.setattr(gc, "_RUNTIME_INSPECTOR", _StaticRuntimeInspector(snapshot))

    def _raise_asyncio_run(awaitable: object) -> None:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise AssertionError("GC runtime inspection must be awaited directly")

    monkeypatch.setattr(gc.asyncio, "run", _raise_asyncio_run)

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [workspace_id]
    assert plan.candidates[0].reason_code == FAILED_WORKSPACE_NO_WORK
    assert plan.preserved == []


async def test_single_workspace_gc_calls_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="j" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    remover_calls: list[str] = []

    async def _worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        workspace_id_arg = getattr(candidate, "workspace_id", None)
        remover_calls.append(workspace_id_arg)
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
        worktree_remover=_worktree_remover,
    )

    assert result.status == "succeeded"
    assert remover_calls == [workspace_id]
    assert result.worktree_removes[workspace_id].status == "succeeded"


def test_worktree_remove_result_to_dict_with_error():
    result = WorkspaceGCWorktreeRemoveResult(
        status="failed",
        reason_code="GIT_WORKTREE_REMOVE_FAILED",
        error="mirror not accessible",
    )
    payload = result.to_dict()
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "GIT_WORKTREE_REMOVE_FAILED"
    assert payload["error"] == "mirror not accessible"


async def test_run_worktree_remove_skips_when_callback_absent() -> None:
    candidate = WorkspaceGCCandidate(
        workspace_id="ws_no_remover",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime(2026, 4, 26, 12, tzinfo=UTC),
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=Path("/tmp/awf/worktrees/ws_no_remover"),
            exists=True,
            estimated_bytes=1,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=Path("/tmp/awf/compose/ws_no_remover"),
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth",
            path=Path("/tmp/awf/auth/ws_no_remover"),
            exists=False,
            estimated_bytes=0,
        ),
    )

    assert await _run_worktree_remove(candidate, None) is None


async def test_gc_worktree_remover_with_awaitable_callback(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="k" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    async def _async_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_async_worktree_remover,
    )

    assert result.status == "succeeded"
    assert result.worktree_removes[workspace_id].status == "succeeded"


async def test_gc_async_worktree_remover_dry_run_does_not_call(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="l" * 40,
    )
    remover_calls: list[str] = []

    async def _async_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        remover_calls.append("called")
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=False,
        now=now,
        worktree_remover=_async_worktree_remover,
    )

    assert result.dry_run is True
    assert remover_calls == []


async def test_single_workspace_gc_preserves_completed_workspace_with_unmerged_pr(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha=None,
    )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.plan.candidates == []
    assert result.deleted_paths == []
    assert result.reservation_releases == {}
    preserved_ids = {p.workspace_id for p in result.plan.preserved}
    assert workspace_id in preserved_ids


@pytest.mark.unit
async def test_plan_with_limit_includes_preserved_rows(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    unmerged_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha=None,
        title="unmerged pr workspace",
    )
    recent_merged_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2),
        pr=True,
        pr_merge_sha="a" * 40,
        title="recent merged workspace",
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        limit=10,
        now=now,
    )

    assert plan.preserved_count >= 1
    preserved_ids = {p.workspace_id for p in plan.preserved}
    assert unmerged_id in preserved_ids
    assert recent_merged_id in preserved_ids


@pytest.mark.unit
async def test_gc_with_sync_worktree_remover_callback(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="m" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    remover_calls: list[str] = []

    def _sync_worktree_remover(
        candidate: object,
    ) -> WorkspaceGCWorktreeRemoveResult:
        workspace_id_arg = getattr(candidate, "workspace_id", None)
        remover_calls.append(workspace_id_arg)
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        worktree_remover=_sync_worktree_remover,
    )

    assert result.status == "succeeded"
    assert remover_calls == [workspace_id]
    assert result.worktree_removes[workspace_id].status == "succeeded"


@pytest.mark.unit
async def test_single_workspace_gc_uses_default_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="n" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    with patch("awf.service.gc._default_worktree_remover") as mock_default:
        mock_default.return_value = WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )
        result = await run_workspace_filesystem_gc(
            session_factory,
            work_dir=work_dir,
            workspace_id=workspace_id,
            execute=True,
            min_age_hours=24,
            now=now,
        )
        assert result.status == "succeeded"
        assert workspace_id in result.worktree_removes
        assert result.worktree_removes[workspace_id].status == "succeeded"
        mock_default.assert_awaited_once()


@pytest.mark.unit
async def test_run_terminal_workspace_gc_uses_default_worktree_remover(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="o" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    with patch("awf.service.gc._default_worktree_remover") as mock_default:
        mock_default.return_value = WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )
        assert result.status == "succeeded"
        assert workspace_id in result.worktree_removes
        assert result.worktree_removes[workspace_id].status == "succeeded"
        mock_default.assert_awaited_once()


@pytest.mark.unit
async def test_plan_reclassifies_old_failed_active_workspace_from_candidate_to_preserved(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    failed_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == failed_id)
            .values(
                compose_project_name="awf_reclassify_gc",
                updated_at=now - timedelta(hours=200),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()

    monkeypatch.setattr(
        gc,
        "_RUNTIME_INSPECTOR",
        _StaticRuntimeInspector(
            RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent",
                        image="awf-agent",
                        state="running",
                        command="./run_agent.sh",
                    )
                ],
            )
        ),
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        include_statuses=[WorkspaceStatus.failed],
        now=now,
    )

    assert plan.candidates == []
    assert plan.preserved_count >= 1
    preserved_for_ws = [p for p in plan.preserved if p.workspace_id == failed_id]
    assert len(preserved_for_ws) == 1
    assert preserved_for_ws[0].reason_code == TERMINAL_WORKSPACE_RETENTION_EXPIRED


async def test_default_worktree_remover_succeeds(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="p" * 40,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock()
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )
        assert result.status == "succeeded"
        assert result.reason_code == "WORKTREE_REMOVE_SUCCEEDED"
        mock_gm.remove_worktree.assert_awaited_once_with(
            workspace_id=workspace_id,
            repo_url="git@github.com:example/repo.git",
        )


async def test_default_worktree_remover_skips_when_no_repo_url(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        ws = await session.get(Workspace, workspace_id)
        assert ws is not None
        ws.repo_url = ""
        await session.commit()

    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.failed.value,
        updated_at=now,
        age_hours=200,
        reason_code="FAILED_WORKSPACE_NO_WORK",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    result = await _default_worktree_remover(
        candidate,
        session_factory=session_factory,
        work_dir=work_dir,
    )
    assert result.status == "skipped"
    assert result.reason_code == "NO_REPO_URL"


async def test_default_worktree_remover_skips_existing_plain_directory(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="r" * 40,
    )
    worktree_path = work_dir / "git" / "worktrees" / workspace_id
    worktree_path.mkdir(parents=True)
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=worktree_path,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    result = await _default_worktree_remover(
        candidate,
        session_factory=session_factory,
        work_dir=work_dir,
    )

    assert result.status == "skipped"
    assert result.reason_code == "WORKTREE_NOT_GIT_MANAGED"


async def test_default_worktree_remover_handles_git_error(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="q" * 40,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.completed.value,
        updated_at=now,
        age_hours=200,
        reason_code="COMPLETED_PR_RETENTION_EXPIRED",
        worktree=WorkspaceGCPath(
            kind="worktree",
            path=work_dir / "git" / "worktrees" / workspace_id,
            exists=True,
            estimated_bytes=0,
        ),
        compose=WorkspaceGCPath(
            kind="compose",
            path=work_dir / "compose" / workspace_id,
            exists=False,
            estimated_bytes=0,
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / workspace_id, exists=False, estimated_bytes=0
        ),
    )

    with patch("awf.node.git_manager.GitManager") as mock_gm_cls:
        mock_gm = mock_gm_cls.return_value
        mock_gm.remove_worktree = AsyncMock(side_effect=RuntimeError("mirror missing"))
        result = await _default_worktree_remover(
            candidate,
            session_factory=session_factory,
            work_dir=work_dir,
        )
        assert result.status == "failed"
        assert result.reason_code == "GIT_WORKTREE_REMOVE_FAILED"
        assert "mirror missing" in result.error


async def test_release_gc_reservations_rollback_on_db_exception(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    ws_a = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="ra" * 20,
    )
    ws_b = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="rb" * 20,
    )
    for wid in (ws_a, ws_b):
        _write(work_dir / "git" / "worktrees" / wid / "repo.txt", "repo")

    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        attempt_id = await _task_attempt_for_workspace(session, ws_b)
        await repo.create(
            workspace_id=ws_b,
            attempt_id=attempt_id,
            node_id="node_b1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=1024,
            phase="steady",
            reserved_at=now - timedelta(hours=300),
        )
        await session.commit()

    call_count = 0
    original_release = ResourceReservationRepository.release_active_for_workspace

    async def _flaky_release(
        self: ResourceReservationRepository, workspace_id: str, **kwargs: object
    ) -> list[ResourceReservation]:
        nonlocal call_count
        call_count += 1
        if workspace_id == ws_a:
            raise RuntimeError("simulated db error")
        return await original_release(self, workspace_id, **kwargs)

    with patch.object(
        ResourceReservationRepository, "release_active_for_workspace", _flaky_release
    ):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    assert result.status == "partial"
    assert ws_a in result.reservation_releases
    assert result.reservation_releases[ws_a].get("error") is not None
    assert ws_b in result.reservation_releases
    assert result.reservation_releases[ws_b].get("released_count", 0) >= 1


async def test_release_gc_reservations_session_isolation_on_db_error(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_a = "ws_session_iso_a"
    ws_b = "ws_session_iso_b"
    original_release = ResourceReservationRepository.release_active_for_workspace

    async def _taint_then_fail(
        self: ResourceReservationRepository, workspace_id: str, **kwargs: object
    ) -> list[ResourceReservation]:
        if workspace_id == ws_a:
            raise RuntimeError("simulated db error that taints session")
        return await original_release(self, workspace_id, **kwargs)

    with patch.object(
        ResourceReservationRepository, "release_active_for_workspace", _taint_then_fail
    ):
        summaries = await _release_gc_reservations(
            session_factory,
            workspace_ids=[ws_a, ws_b],
        )

    assert ws_a in summaries
    assert summaries[ws_a].get("error") is not None
    assert summaries[ws_a].get("released_count") == 0
    assert ws_b in summaries
    assert summaries[ws_b].get("error") is None
