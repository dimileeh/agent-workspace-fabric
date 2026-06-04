from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.gc import (
    COMPLETED_PR_NOT_MERGED,
    FAILED_WORKSPACE_TRIAGE_PRESERVED,
    WORKSPACE_WITHIN_RETENTION,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCWorktreeRemoveResult,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)

"""Terminal workspace filesystem GC tests."""


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


async def _set_workspace_gc_state(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    **values: object,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def _issue_gc_secret_lease(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    now: datetime,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        await SecretLeaseRepository(session).issue_declared_leases(
            workspace,
            leases=[
                SecretLeaseIssue(
                    secret_name="api-token",
                    kind="env",
                    target="API_TOKEN",
                    mode="ro",
                    required=True,
                    provider="env",
                    ref_digest="sha256:" + "e" * 64,
                    expires_at=now + timedelta(hours=1),
                    issue_metadata={"profile": "gc", "declaration_index": 0},
                )
            ],
            now=now,
        )
        await session.commit()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _StaticRuntimeInspector:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshot = snapshot

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        assert compose_project_name is not None
        return self.snapshot


@pytest.mark.unit
async def test_single_workspace_gc_cleanup_disabled_skips_fallback_compose_teardown(
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
        pr_merge_sha="a" * 40,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    calls: list[str] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        cleanup_enabled=False,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        "WORKSPACE_CLEANUP_DISABLED",
    ]
    assert calls == []
    assert result.compose_teardowns == {}
    assert result.secret_lease_revocations == {}
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
    assert leases[0].status == "issued"


@pytest.mark.unit
async def test_single_workspace_gc_failed_within_retention_skips_fallback_compose_teardown(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
    )
    calls: list[str] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        result = await run_workspace_filesystem_gc(
            session_factory,
            work_dir=work_dir,
            workspace_id=workspace_id,
            execute=True,
            compose_teardown=_compose_teardown,
            now=now,
        )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        WORKSPACE_WITHIN_RETENTION,
    ]
    assert calls == []
    assert result.compose_teardowns == {}


@pytest.mark.unit
async def test_single_workspace_gc_triage_preserved_skips_fallback_compose_teardown(
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
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    calls: list[str] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        FAILED_WORKSPACE_TRIAGE_PRESERVED,
    ]
    assert calls == []
    assert result.compose_teardowns == {}
    assert result.secret_lease_revocations == {}
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
    assert leases[0].status == "issued"


@pytest.mark.unit
async def test_single_workspace_unmerged_pr_skips_fallback_compose_and_runtime_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        pr=True,
        pr_merge_sha=None,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
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
            reserved_at=now - timedelta(hours=1),
        )
        await session.commit()

    async def _compose_teardown(
        _candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    calls: list[str] = []

    async def _tracked_compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return await _compose_teardown(candidate)

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        ignore_retention=True,
        compose_teardown=_tracked_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        COMPLETED_PR_NOT_MERGED,
    ]
    assert calls == []
    assert result.compose_teardowns == {}
    assert result.secret_lease_revocations == {}
    assert result.reservation_releases == {}
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
        reservation = await ResourceReservationRepository(session).active_for_workspace(
            workspace_id
        )

    assert leases[0].status == "issued"
    assert leases[0].revoke_reason_code is None
    assert reservation is not None


@pytest.mark.unit
async def test_single_workspace_gc_revokes_active_secret_leases_before_auth_cleanup(
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
        pr_merge_sha="a" * 40,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    auth = work_dir / "auth" / workspace_id
    _write(auth / "codex" / "auth.json", "auth")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert not auth.exists()
    assert result.to_dict()["secret_leases"] == {
        workspace_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"}
    }
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.secret_lease",
            limit=10,
        )

    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_GC"
    assert {event.reason_code for event in events} == {
        "SECRET_LEASE_ISSUED",
        "SECRET_LEASE_REVOKED",
    }


@pytest.mark.unit
async def test_batch_terminal_gc_compose_teardown_failure_blocks_runtime_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    failed_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=210),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    released_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="b" * 40,
    )
    await _issue_gc_secret_lease(session_factory, failed_id, now=now)
    await _issue_gc_secret_lease(session_factory, released_id, now=now)
    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        failed_attempt_id = await _task_attempt_for_workspace(session, failed_id)
        released_attempt_id = await _task_attempt_for_workspace(session, released_id)
        for workspace_id, attempt_id in (
            (failed_id, failed_attempt_id),
            (released_id, released_attempt_id),
        ):
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

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        if candidate.workspace_id == failed_id:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="network still in use",
            )
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert result.status == "partial"
    assert failed_id in result.compose_teardowns
    assert released_id in result.compose_teardowns
    assert failed_id not in result.secret_lease_revocations
    assert failed_id not in result.reservation_releases
    assert result.secret_lease_revocations == {
        released_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"}
    }
    assert result.reservation_releases[released_id]["released_count"] == 1
    async with session_factory() as session:
        failed_leases = await SecretLeaseRepository(session).list_for_workspace(failed_id)
        released_leases = await SecretLeaseRepository(session).list_for_workspace(released_id)
        failed_reservation = await ResourceReservationRepository(session).active_for_workspace(
            failed_id
        )
        released_reservation = await ResourceReservationRepository(session).active_for_workspace(
            released_id
        )

    assert failed_leases[0].status == "issued"
    assert released_leases[0].status == "revoked"
    assert failed_reservation is not None
    assert released_reservation is None


@pytest.mark.unit
async def test_batch_terminal_gc_revokes_each_candidate_and_is_retry_safe(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    first_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    second_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=210),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    await _issue_gc_secret_lease(session_factory, first_id, now=now)
    await _issue_gc_secret_lease(session_factory, second_id, now=now)
    first_auth = work_dir / "auth" / first_id
    second_auth = work_dir / "auth" / second_id
    _write(first_auth / "codex" / "auth.json", "auth")
    _write(second_auth / "codex" / "auth.json", "auth")
    first_worktree = work_dir / "git" / "worktrees" / first_id
    first_worktree.parent.mkdir(parents=True, exist_ok=True)
    first_worktree.write_text("not a directory", encoding="utf-8")

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=now,
    )
    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert first.status == "partial"
    assert first.to_dict()["secret_leases"] == {
        first_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"},
        second_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"},
    }
    assert second.to_dict()["secret_leases"] == {
        first_id: {"revoked_count": 0, "reason_code": "TERMINAL_GC"},
        second_id: {"revoked_count": 0, "reason_code": "TERMINAL_GC"},
    }
    async with session_factory() as session:
        first_leases = await SecretLeaseRepository(session).list_for_workspace(first_id)
        second_leases = await SecretLeaseRepository(session).list_for_workspace(second_id)

    assert first_leases[0].status == "revoked"
    assert second_leases[0].status == "revoked"
