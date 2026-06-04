"""PR monitor completion filesystem cleanup tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeTeardownResult
from awf.service.gc import (
    WorkspaceCleanupExecutionStatus,
    WorkspaceGCCandidate,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCPath,
    WorkspaceGCPlan,
    WorkspaceGCPreserved,
    WorkspaceGCResult,
    WorkspaceGCWorktreeRemoveResult,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    mock_completed_compose_manager,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mock_worktree_remove_success() -> object:
    return patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    )


def _empty_workspace_gc_result(
    work_dir: Path,
    *,
    workspace_id: str | None = None,
    include_statuses: tuple[str, ...] = (WorkspaceStatus.completed.value,),
    preserved: list[WorkspaceGCPreserved] | None = None,
    compose_teardown: WorkspaceGCComposeTeardownResult | None = None,
    status: WorkspaceCleanupExecutionStatus = "succeeded",
    reason_code: str = "CLEANUP_EXECUTION_SUCCEEDED",
) -> WorkspaceGCResult:
    now = datetime.now(UTC)
    return WorkspaceGCResult(
        plan=WorkspaceGCPlan(
            work_dir=work_dir,
            min_age_hours=24,
            cutoff_at=now,
            include_statuses=include_statuses,
            exclude_statuses=(),
            candidates=[],
            preserved=preserved or [],
        ),
        dry_run=False,
        deleted_paths=[],
        delete_errors=[],
        path_outcomes=[],
        compose_teardowns=(
            {workspace_id: compose_teardown}
            if workspace_id is not None and compose_teardown is not None
            else {}
        ),
        secret_lease_revocations={},
        worktree_removes={},
        reservation_releases={},
        status=status,
        reason_code=reason_code,
    )


async def _issue_monitor_secret_lease(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    now: datetime,
) -> None:
    async with factory() as session:
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
                    ref_digest="sha256:" + "f" * 64,
                    expires_at=now + timedelta(hours=1),
                    issue_metadata={"profile": "monitor", "declaration_index": 0},
                )
            ],
            now=now,
        )
        await session.commit()


@pytest.mark.unit
async def test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"
    ws_id = await seed_monitoring_workspace(factory)
    now = datetime.now(UTC)
    await _issue_monitor_secret_lease(factory, ws_id, now=now)
    worktree = worktrees_root / ws_id
    compose_dir = work_dir / "compose" / ws_id
    auth = work_dir / "auth" / ws_id
    _write(worktree / "repo.txt", "repo")
    _write(compose_dir / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )
    compose_patch, compose_calls = mock_completed_compose_manager(
        ComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_UNAVAILABLE",
            error="docker unavailable",
        )
    )
    with compose_patch, structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=compose_dir / "compose.yml",
        )

    assert worktree.exists()
    assert auth.exists()
    assert compose_calls == [(f"awf_{ws_id}", Path(f"/tmp/awf/{ws_id}/compose.yml"), ws_id, True)]
    assert not any(call.args[:2] == ["docker", "compose"] for call in cmd.calls)
    assert any(
        record.get("event") == "monitor.compose_teardown_failed"
        and record.get("workspace_id") == ws_id
        and record.get("reason_code") == "DOCKER_UNAVAILABLE"
        for record in captured
    )
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        leases = await SecretLeaseRepository(session).list_for_workspace(ws_id)
        assert leases[0].status == "issued"
        assert leases[0].revoke_reason_code is None


@pytest.mark.unit
async def test_completed_monitor_filesystem_gc_logs_failure_on_reservation_release_error(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    from awf.service.gc import (
        WorkspaceGCPlan,
        WorkspaceGCResult,
    )

    ws_id = "ws-resv-err"
    work_dir = tmp_path / "service"
    worktrees_root = work_dir / "git" / "worktrees"

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=worktrees_root,
    )

    now = datetime.now(UTC) - timedelta(days=30)
    candidate = WorkspaceGCCandidate(
        workspace_id=ws_id,
        status="completed",
        updated_at=now,
        age_hours=720,
        reason_code="AGE",
        worktree=WorkspaceGCPath(
            kind="worktree", path=worktrees_root / ws_id, exists=True, estimated_bytes=0
        ),
        compose=WorkspaceGCPath(
            kind="compose", path=work_dir / "compose" / ws_id, exists=True, estimated_bytes=0
        ),
        auth=WorkspaceGCPath(
            kind="auth", path=work_dir / "auth" / ws_id, exists=True, estimated_bytes=0
        ),
    )
    plan = WorkspaceGCPlan(
        work_dir=work_dir,
        min_age_hours=24,
        cutoff_at=now,
        include_statuses=("completed",),
        exclude_statuses=(),
        candidates=[candidate],
        preserved=[],
    )
    fake_result = WorkspaceGCResult(
        plan=plan,
        dry_run=False,
        deleted_paths=[],
        delete_errors=[],
        path_outcomes=[],
        compose_teardowns={},
        secret_lease_revocations={},
        worktree_removes={},
        reservation_releases={
            ws_id: {
                "released_count": 0,
                "reason_code": "TERMINAL_GC",
                "error": "db connection failed",
            }
        },
        status="partial",
        reason_code="CLEANUP_EXECUTION_PARTIAL",
    )

    with (
        patch(
            "awf.runtime.pr_monitor_runner.lifecycle.run_workspace_filesystem_gc",
            new=AsyncMock(return_value=fake_result),
        ),
        structlog.testing.capture_logs() as captured,
    ):
        await runner._gc_completed_workspace_filesystem(ws_id)

    assert any(
        record.get("event") == "monitor.filesystem_gc_failed"
        and record.get("workspace_id") == ws_id
        and record.get("reservation_releases", {}).get(ws_id, {}).get("error") is not None
        for record in captured
    ), (
        f"Expected filesystem_gc_failed with reservation release error; got events: {[r.get('event') for r in captured]}"
    )
    assert not any(record.get("event") == "monitor.filesystem_gc_ok" for record in captured), (
        "filesystem_gc_ok should not be emitted when reservation release fails"
    )
