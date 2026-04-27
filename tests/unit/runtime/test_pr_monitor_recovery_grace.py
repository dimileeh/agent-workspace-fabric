"""Grace-before-recovery ordering for the PR monitor runner.

Covers the rule "initial-review grace wins over stale recovery" and
the structured payload the runner now writes when recovery is finally
dispatched (`recovery_mode = "validate_only"`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.base import Base
from awf.db.enums import OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import (
    AbortReason,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import _initial_review_grace_started_key
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _green_pr_status() -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        ws.auto_merge = auto_merge
        await s.commit()


@pytest.mark.unit
async def test_initial_review_grace_defers_stale_recovery_dispatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Grace must win over the stale-recovery dispatch — no Operation
    is created and the workspace stays in monitoring_pr while grace
    is active. This is the core regression guard for the dogfood
    incident where validation recovery fired during the grace window.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Seed the state with a monotonic timestamp ~now so grace is active.
    state = MonitorState(started_at=time.monotonic())

    # Use the private dispatcher directly so we can control state.
    # By passing a Merge action with a still-active grace window the
    # dispatcher must honour grace before checking stale.
    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_pr_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # Did NOT terminate, did NOT create an Operation, did NOT transition.
    assert terminal is False
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert operations == []

    # Slept for the grace remainder (capped by poll_interval=60).
    assert sleep_fn.calls == [60]

    # Recovery deferral surfaced as a workspace event so operators can
    # diagnose the wait without tailing logs.
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = [e for e in ws.events if e.event_type == "monitor.grace_defers_recovery"]
        assert len(events) == 1
        payload = events[0].payload or {}
        assert payload.get("stale_reason") == "validation_insufficient_tier"
        assert payload.get("req_action") == "validate"

    # The grace bookkeeping key is in-memory on the state; the main
    # ``run()`` loop calls ``_persist_state`` after the dispatcher
    # returns, but here we exercise ``_execute`` directly so we only
    # check the in-memory mutation.
    assert _initial_review_grace_started_key(42) in state.threads_addressed_ids


@pytest.mark.unit
async def test_grace_elapsed_then_stale_recovery_dispatches_with_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """After grace elapses, the runner dispatches recovery with a
    `recovery_mode="validate_only"` payload discriminator and emits a
    `monitor.recovery_dispatched` event."""
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Pre-mark grace as already elapsed.
    state = MonitorState(started_at=0.0)
    state.mark_addressed(
        _initial_review_grace_started_key(42),
        f"{0.0:.6f}",
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_pr_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
    assert len(operations) == 1
    operation = operations[0]
    assert operation.type == OperationType.validate.value
    assert operation.payload == {
        "source": "pr_monitor",
        "reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
    }

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert len(events) == 1
        payload = events[0].payload or {}
        assert payload.get("reason") == "validation_insufficient_tier"
        assert payload.get("req_action") == "validate"
        assert payload.get("recovery_mode") == "validate_only"
        assert payload.get("pr_number") == 42


@pytest.mark.unit
async def test_manual_merge_mode_aborts_stale_regardless_of_grace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """auto_merge=False must take the Abort(stale) path — grace is
    irrelevant for manual-merge mode (the existing dogfood semantics
    must remain intact)."""
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=False)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Grace would still be active (started_at ~now), but manual merge
    # mode must abort instead of waiting.
    state = MonitorState(started_at=time.monotonic())

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_green_pr_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_message == f"monitor: abort ({AbortReason.stale.value})"
        # No recovery operation was created.
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert operations == []
        # No grace-defer event fired in manual merge.
        events = [e for e in ws.events if e.event_type == "monitor.grace_defers_recovery"]
        assert events == []


@pytest.mark.unit
async def test_failed_rebase_with_stale_notifies_human_under_grace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When `compute_stale_reason` returns `(_, "rebase")` and a prior
    rebase op failed, the runner posts a NotifyHuman comment — grace
    must NOT suppress that, since the workspace is unrecoverable
    without operator help."""
    import datetime

    from sqlalchemy.exc import StatementError  # noqa: F401

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)

    # Seed a failed rebase operation so the rebase guard fires.
    async with factory() as s:
        await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            payload={"source": "operator"},
        )
        # Mark it failed via direct UPDATE, bypassing the .finish() helper
        # which expects an existing operation object.
        from sqlalchemy import update

        from awf.db.models import Operation

        await s.execute(
            update(Operation)
            .where(Operation.workspace_id == workspace_id)
            .values(status="failed", finished_at=datetime.datetime.now(datetime.UTC)),
        )
        await s.commit()

    cmd.queue_result(returncode=0)  # gh pr comment

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Force compute_stale_reason to claim the action is "rebase" —
    # otherwise the default refactor path returns "validate".
    import awf.runtime.pr_monitor_runner as pr_monitor_runner

    original_compute = pr_monitor_runner.__dict__.get("compute_stale_reason")
    monkeypatched: dict[str, object] = {}

    def fake_compute_stale_reason(ws):  # type: ignore[no-untyped-def]
        return ("validation_insufficient_tier", "rebase")

    # Patch the imported reference inside the function; the runner
    # imports compute_stale_reason inline so we patch via its module.
    import awf.runtime.merge_eligibility as merge_eligibility

    original_module_compute = merge_eligibility.compute_stale_reason
    merge_eligibility.compute_stale_reason = fake_compute_stale_reason  # type: ignore[assignment]
    try:
        state = MonitorState(started_at=time.monotonic())
        terminal = await runner._execute(
            action=Merge(),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_green_pr_status(),
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )
    finally:
        merge_eligibility.compute_stale_reason = original_module_compute  # type: ignore[assignment]
        del monkeypatched
        del original_compute

    assert terminal is False
    # NotifyHuman fired (gh pr comment).
    assert cmd.calls
    assert cmd.calls[0].args[:3] == ["gh", "pr", "comment"]
    # No recovery operation was created (NotifyHuman short-circuits before
    # the dispatch block).
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        # Only the seeded rebase op exists.
        ops = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert [op.type for op in ops] == [OperationType.rebase.value]
