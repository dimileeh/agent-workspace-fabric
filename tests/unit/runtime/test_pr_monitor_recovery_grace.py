"""Grace-before-recovery ordering for the PR monitor runner.

Covers the rule "initial-review grace wins over stale recovery" and
the structured payload the runner now writes when recovery is finally
dispatched (`recovery_mode = "validate_only"`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.logs import LogStore
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_operations import monitor_operation_idempotency_key
from awf.runtime.pr_monitor_runner.helpers import _initial_review_grace_started_key
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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


def _elapsed_initial_review_grace_state(
    *,
    pr_number: int = 42,
    grace_seconds: float = 900.0,
) -> MonitorState:
    """Return monitor state whose one-time review grace has definitely elapsed."""

    started_at = time.monotonic() - grace_seconds - 1.0
    state = MonitorState(started_at=started_at)
    state.mark_addressed(_initial_review_grace_started_key(pr_number), f"{started_at:.6f}")
    return state


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


async def _mark_candidate_target_advanced_stale(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    async with factory() as s:
        candidate = await MergeCandidateRepository(s).get_open_for_workspace_with_merge_inputs(
            workspace_id
        )
        assert candidate is not None
        ws = candidate.workspace
        started_at = datetime.now(UTC)
        validation_run = await ValidationRunRepository(s).start(
            workspace_id=workspace_id,
            attempt_id=candidate.attempt_id,
            tier=2,
            commands=[],
            base_commit=ws.base_commit,
            target_branch=ws.remote_push_branch,
            target_head_sha=candidate.head_sha,
            log_stream_refs={},
            started_at=started_at,
        )
        await ValidationRunRepository(s).finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            finished_at=started_at + timedelta(seconds=1),
        )
        candidate.stale = True
        candidate.stale_reason = "STALE_TARGET_ADVANCED"
        await s.commit()


@pytest.mark.unit
async def test_initial_review_grace_defers_stale_recovery_dispatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Grace must win over the stale-recovery dispatch — only a monitor-state
    wait operation is created and the workspace stays in monitoring_pr while grace
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
        assert len(operations) == 1
        operation = operations[0]
        assert operation.type == "monitor_state"
        assert operation.status == OperationStatus.succeeded.value
        assert operation.payload["action"] == "grace_wait"
        assert operation.payload["reason_code"] == "INITIAL_REVIEW_GRACE"
        assert operation.payload["stale_reason"] == "validation_insufficient_tier"
        assert operation.payload["requested_action"] == "validate"

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
async def test_grace_defers_recovery_clears_stale_awaiting_human_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The initial-review grace wait inside ``_handle_merge_gate_blocker`` must
    clear a stale ``awaiting_human_since`` (#659).

    When a prior ``HUMAN_WAIT`` is resolved and the next poll returns ``Merge``,
    ``loop._execute`` skips its general attention clear for the ``Merge`` arm. If
    the merge gate then requires validation recovery but the grace window is still
    active (``non_check_reviewer_settle_seconds == 0`` so the grace is not skipped),
    ``_handle_merge_gate_blocker`` parks on an ``INITIAL_REVIEW_GRACE`` wait before
    recovery. That non-human grace wait must clear the stale flag itself, otherwise
    the console/KPI/metrics — surfaced on ``(status == monitoring_pr AND
    awaiting_human_since IS NOT NULL)`` — keep showing "awaiting human" while the
    monitor is merely waiting out the grace period.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)

    # Seed a stable episode start from an earlier, now-resolved NotifyHuman poll.
    episode_start = datetime(2026, 4, 26, 11, 0, tzinfo=UTC)
    async with factory() as s:
        await WorkspaceRepository(s).set_workspace_attention(
            workspace_id, reason="prior human escalation", now=episode_start
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

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

    # The monitor parked on the grace wait (non-human gate), not a merge.
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert len(operations) == 1
        assert operations[0].payload["reason_code"] == "INITIAL_REVIEW_GRACE"
        # Still polling, but the stale awaiting-human signal is cleared.
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None


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
    state = _elapsed_initial_review_grace_state()

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
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
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
async def test_manual_merge_mode_dispatches_validation_before_handoff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """auto_merge=False still needs final validation before human handoff."""
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
        initial_review_grace_period_seconds=0,
    )

    # Manual merge mode must dispatch validation instead of handing off stale evidence.
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
        assert ws.status == WorkspaceStatus.ready.value
        assert ws.failure_reason is None
        assert ws.failure_message is None
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        validate_operations = [op for op in operations if op.type == OperationType.validate.value]
        assert len(validate_operations) == 1
        assert validate_operations[0].payload["recovery_mode"] == "validate_only"
        assert validate_operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
        events = [e for e in ws.events if e.event_type == "monitor.grace_defers_recovery"]
        assert events == []


@pytest.mark.unit
async def test_failed_rebase_with_stale_notifies_human_under_grace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When target-advanced stale recovery needs rebase and a prior rebase
    op failed, the runner posts a NotifyHuman comment — grace must NOT
    suppress that, since the workspace is unrecoverable without operator
    help."""

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    await _mark_candidate_target_advanced_stale(factory, workspace_id)

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
            .values(status="failed", finished_at=datetime.now(UTC)),
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

    assert terminal is False
    # NotifyHuman fired (gh pr comment).
    assert cmd.calls
    assert cmd.calls[0].args[:3] == ["gh", "pr", "comment"]
    # No validate recovery operation was created; NotifyHuman records the
    # durable human-wait operation instead.
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        ops = await OperationRepository(s).list_all(workspace_id=workspace_id)
        assert sorted(op.type for op in ops) == [
            OperationType.human_wait.value,
            OperationType.rebase.value,
        ]


@pytest.mark.unit
async def test_remonitor_after_failed_rebase_allows_fresh_recovery_dispatch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    await _mark_candidate_target_advanced_stale(factory, workspace_id)

    async with factory() as s:
        operations = OperationRepository(s)
        failed = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            payload={
                "source": "pr_monitor",
                "reason": "STALE_TARGET_ADVANCED",
                "recovery_mode": "rebase_only",
            },
        )
        await operations.finish(
            failed,
            status=OperationStatus.failed,
            result={"reason_code": "MONITOR_RECOVERY_REBASE_FAILED"},
        )
        await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.succeeded,
            payload={"reason": "reattach after fixing recovery"},
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    state = _elapsed_initial_review_grace_state()
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
    assert cmd.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        ops = await OperationRepository(s).list_all(workspace_id=workspace_id)

    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert [op.status for op in pr_monitor_ops] == [
        OperationStatus.pending.value,
        OperationStatus.failed.value,
    ]


@pytest.mark.unit
async def test_rebase_req_action_dispatches_validate_typed_recovery_op(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When ``req_action == "rebase"`` and no prior failed rebase exists,
    the dispatch must still create a ``validate``-typed Operation row.

    The executor's recovery branch only runs validation today, and its
    success/failure paths close pending ops via the validate finisher
    (which queries by ``OperationType.validate``). A ``rebase``-typed
    row would therefore leak forever as ``running``. ``recovery_mode``
    in the payload remains the discriminator for the future rebase
    slice.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    await _mark_candidate_target_advanced_stale(factory, workspace_id)

    cmd.queue_result(returncode=0)  # any optional gh call

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Pre-mark grace as elapsed so the dispatch block runs.
    state = _elapsed_initial_review_grace_state()
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
    # Operation type is ``validate`` even though req_action was ``rebase``
    # — otherwise the validate finisher (which queries by type) would
    # never close it on the executor's success path.
    assert operation.type == OperationType.validate.value
    # The ``rebase_only`` recovery_mode survives in the payload as the
    # discriminator the future rebase slice can read.
    assert operation.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "rebase_only",
        "requested_action": "rebase",
        "reason": "Target branch advanced after this merge candidate was validated.",
        "reason_code": "STALE_TARGET_ADVANCED",
        "stale_reason": "STALE_TARGET_ADVANCED",
        "recovery_mode": "rebase_only",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }


@pytest.mark.unit
async def test_monitor_recovery_operation_includes_monitor_log_ref_when_available(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    log_store = LogStore(root=tmp_path / "logs", session_factory=factory)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        log_store=log_store,
    )

    monitor_log = await runner._open_monitor_log(workspace_id)
    state = _elapsed_initial_review_grace_state()
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
        monitor_log=monitor_log,
    )
    if monitor_log is not None:
        await monitor_log.close()

    assert terminal is True
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
    assert operations[0].payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "log_stream_refs": {"monitor": "monitor.log"},
    }


@pytest.mark.unit
async def test_recovery_dispatch_is_idempotent_when_active_recovery_op_exists(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A second dispatch must NOT create a duplicate pr_monitor op when
    one is already active. Defends against a runner restart re-entering
    the dispatch branch before the executor has finished the prior op.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)

    # Pre-seed an active (pending) pr_monitor recovery op so the runner
    # finds it on entry to the dispatch block.
    async with factory() as s:
        await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "requested_action": "validate",
                "recovery_mode": "validate_only",
            },
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )

    # Pre-mark grace as elapsed so the dispatch block runs.
    state = _elapsed_initial_review_grace_state()

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

    assert terminal is False
    assert sleep_fn.calls == [runner._config.poll_interval_seconds]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Workspace stays in monitoring_pr — the existing pending op is
        # owned by an earlier dispatch that already transitioned (or is
        # racing to transition); the duplicate-create guard MUST NOT
        # transition again or exit the monitor as terminal.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        # The pre-seeded operation is preserved and a monitor_state wait
        # operation records why the monitor stayed alive.
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        wait_operations = [
            op
            for op in operations
            if op.type == OperationType.monitor_state.value
            and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
        ]
        assert len(recovery_operations) == 1
        assert len(wait_operations) == 1
        assert recovery_operations[0].payload == {
            "owner": "pr_monitor",
            "source": "pr_monitor",
            "reason": "Required validation tier has not passed for this merge candidate.",
            "reason_code": "VALIDATION_INSUFFICIENT_TIER",
            "stale_reason": "validation_insufficient_tier",
            "requested_action": "validate",
            "recovery_mode": "validate_only",
        }
        assert recovery_operations[0].status == OperationStatus.pending.value
        assert wait_operations[0].status == OperationStatus.succeeded.value
        # No monitor.recovery_dispatched event fires the second time
        # (the original dispatch already emitted it).
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "operation_type",
    [
        OperationType.sync_base,
        OperationType.ci_repair,
        OperationType.comment_repair,
        OperationType.human_wait,
    ],
)
async def test_stale_recovery_callback_ignores_active_pr_monitor_non_recovery_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    operation_type: OperationType,
) -> None:
    """Non-recovery monitor work must not keep a completed dispatch callback live."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(
            ws,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY_AFTER_RECOVERY_DISPATCH",
        )
        await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=OperationStatus.running,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": operation_type.value,
                "requested_action": operation_type.value,
                "reason_code": operation_type.value.upper(),
            },
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    stale = await runner._recovery_dispatch_status_is_stale(workspace_id)

    assert stale is True
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        stale_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
    assert len(stale_events) == 1
    assert stale_events[0].reason_code == "STALE_CALLBACK_IGNORED"
    assert stale_events[0].payload["callback_action"] == "recovery_dispatch"
    assert stale_events[0].payload["actual_status"] == WorkspaceStatus.ready.value


@pytest.mark.unit
async def test_recovery_dispatch_does_not_requeue_already_succeeded_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A terminal recovery op for the same observed PR snapshot means the
    recovery was already handled. The monitor must keep polling instead of
    moving the workspace back to ready without an active operation for the
    executor to consume.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    idempotency_key = monitor_operation_idempotency_key(
        workspace_id=workspace_id,
        action="validate_only",
        pr_number=42,
        reason_code="VALIDATION_INSUFFICIENT_TIER",
        source_head_sha="abc1234567890def",
        source_base_sha="a" * 40,
    )

    async with factory() as s:
        repo = OperationRepository(s)
        operation = await repo.create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
                "pr_number": 42,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
                "source_head_sha": "abc1234567890def",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{workspace_id}",
            },
            idempotency_key=idempotency_key,
        )
        await repo.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"reason_code": "VALIDATION_OK"},
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = _elapsed_initial_review_grace_state()

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

    assert terminal is False
    assert sleep_fn.calls == [runner._config.poll_interval_seconds]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        wait_operations = [
            op
            for op in operations
            if op.type == OperationType.monitor_state.value
            and op.payload.get("reason_code") == "RECOVERY_SNAPSHOT_ALREADY_HANDLED"
        ]
        assert len(recovery_operations) == 1
        assert recovery_operations[0].status == OperationStatus.succeeded.value
        assert len(wait_operations) == 1
        assert wait_operations[0].payload["action"] == "recovery_already_handled"
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert events == []


@pytest.mark.unit
async def test_recovery_dispatch_retries_cancelled_idempotent_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A cancelled snapshot is retryable and must produce a fresh recovery operation."""
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    idempotency_key = monitor_operation_idempotency_key(
        workspace_id=workspace_id,
        action="validate_only",
        pr_number=42,
        reason_code="VALIDATION_INSUFFICIENT_TIER",
        source_head_sha="abc1234567890def",
        source_base_sha="a" * 40,
    )

    async with factory() as s:
        repo = OperationRepository(s)
        operation = await repo.create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
                "pr_number": 42,
                "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
                "source_head_sha": "abc1234567890def",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{workspace_id}",
            },
            idempotency_key=idempotency_key,
        )
        await repo.finish(
            operation,
            status=OperationStatus.cancelled,
            result={"reason_code": "OPERATOR_REMONITOR"},
            error_code="OPERATOR_REMONITOR",
        )
        await s.commit()

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = _elapsed_initial_review_grace_state()

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
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        wait_operations = [
            op
            for op in operations
            if op.type == OperationType.monitor_state.value
            and op.payload.get("reason_code") == "RECOVERY_SNAPSHOT_ALREADY_HANDLED"
        ]
        assert len(recovery_operations) == 2
        assert len(wait_operations) == 0
        retry = next(op for op in recovery_operations if op.status == OperationStatus.pending.value)
        cancelled = next(
            op for op in recovery_operations if op.status == OperationStatus.cancelled.value
        )
        assert cancelled.idempotency_key == idempotency_key
        assert retry.idempotency_key != idempotency_key
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert len(events) == 1


@pytest.mark.unit
async def test_recovery_dispatch_waits_when_idempotent_create_loses_race_to_active_op(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If another monitor loop creates the recovery op after the active-op
    precheck, the idempotent create return value must still keep the workspace
    in monitoring_pr.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    original_create_idempotent = OperationRepository.create_idempotent
    inserted = False

    async def _race_create_idempotent(
        self: OperationRepository,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, object] | None = None,
        idempotency_key: str,
    ) -> tuple[object, bool]:
        """Simulate a concurrent recovery create winning before the runner insert."""
        nonlocal inserted
        if not inserted:
            inserted = True
            async with factory() as s:
                await OperationRepository(s).create(
                    workspace_id=workspace_id,
                    operation_type=operation_type,
                    status=OperationStatus.pending,
                    payload={
                        "owner": "pr_monitor",
                        "source": "pr_monitor",
                        "action": "validate_only",
                        "requested_action": "validate",
                        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                        "stale_reason": "validation_insufficient_tier",
                        "recovery_mode": "validate_only",
                    },
                    idempotency_key=idempotency_key,
                )
                await s.commit()
        return await original_create_idempotent(
            self,
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(OperationRepository, "create_idempotent", _race_create_idempotent)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = _elapsed_initial_review_grace_state()

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

    assert terminal is False
    assert sleep_fn.calls == [runner._config.poll_interval_seconds]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        wait_operations = [
            op
            for op in operations
            if op.type == OperationType.monitor_state.value
            and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
        ]
        assert len(recovery_operations) == 1
        assert recovery_operations[0].status == OperationStatus.pending.value
        assert len(wait_operations) == 1
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert events == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("terminal_status", "error_code"),
    [
        (OperationStatus.failed, "COMMAND_FAILED"),
        (OperationStatus.cancelled, "OPERATOR_REMONITOR"),
    ],
)
async def test_recovery_dispatch_retries_when_idempotent_create_loses_race_to_terminal_op(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: OperationStatus,
    error_code: str,
) -> None:
    """A competing monitor can finish the same recovery key before our insert.

    The returned terminal operation must feed the retryable idempotency-key flow
    immediately, not get classified as an already-handled snapshot.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    original_create_idempotent = OperationRepository.create_idempotent
    inserted = False

    async def _race_create_idempotent(
        self: OperationRepository,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, object] | None = None,
        idempotency_key: str,
    ) -> tuple[object, bool]:
        """Simulate a concurrent recovery create and terminal finish winning."""
        nonlocal inserted
        if not inserted:
            inserted = True
            async with factory() as s:
                repo = OperationRepository(s)
                operation = await repo.create(
                    workspace_id=workspace_id,
                    operation_type=operation_type,
                    status=OperationStatus.running,
                    payload={
                        "owner": "pr_monitor",
                        "source": "pr_monitor",
                        "action": "validate_only",
                        "requested_action": "validate",
                        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                        "stale_reason": "validation_insufficient_tier",
                        "recovery_mode": "validate_only",
                    },
                    idempotency_key=idempotency_key,
                )
                await repo.finish(
                    operation,
                    status=terminal_status,
                    result={"reason_code": error_code},
                    error_code=error_code,
                )
                await s.commit()
        return await original_create_idempotent(
            self,
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(OperationRepository, "create_idempotent", _race_create_idempotent)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = _elapsed_initial_review_grace_state()

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
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        wait_operations = [
            op
            for op in operations
            if op.type == OperationType.monitor_state.value
            and op.payload.get("reason_code") == "RECOVERY_SNAPSHOT_ALREADY_HANDLED"
        ]
        assert len(recovery_operations) == 2
        assert len(wait_operations) == 0
        retry = next(op for op in recovery_operations if op.status == OperationStatus.pending.value)
        terminal_recovery = next(
            op for op in recovery_operations if op.status == terminal_status.value
        )
        assert retry.idempotency_key != terminal_recovery.idempotency_key
        events = [e for e in ws.events if e.event_type == "monitor.recovery_dispatched"]
        assert len(events) == 1


@pytest.mark.unit
async def test_recovery_dispatch_rechecks_workspace_before_retrying_idempotency_key(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retryable terminal recovery op does not justify dispatching if
    ownership was lost before the retry key is used.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_refactor_task(factory, workspace_id, auto_merge=True)
    original_create_idempotent = OperationRepository.create_idempotent
    inserted = False

    async def _race_create_idempotent(
        self: OperationRepository,
        *,
        workspace_id: str,
        operation_type: OperationType | str,
        status: OperationStatus | str = OperationStatus.pending,
        payload: dict[str, object] | None = None,
        idempotency_key: str,
    ) -> tuple[object, bool]:
        """Simulate another actor finishing recovery and completing the workspace."""
        nonlocal inserted
        if not inserted:
            inserted = True
            async with factory() as s:
                repo = OperationRepository(s)
                operation = await repo.create(
                    workspace_id=workspace_id,
                    operation_type=operation_type,
                    status=OperationStatus.running,
                    payload={
                        "owner": "pr_monitor",
                        "source": "pr_monitor",
                        "action": "validate_only",
                        "requested_action": "validate",
                        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                        "stale_reason": "validation_insufficient_tier",
                        "recovery_mode": "validate_only",
                    },
                    idempotency_key=idempotency_key,
                )
                await repo.finish(
                    operation,
                    status=OperationStatus.cancelled,
                    result={"reason_code": "OPERATOR_REMONITOR"},
                    error_code="OPERATOR_REMONITOR",
                )
                await s.execute(
                    sa_update(Workspace)
                    .where(Workspace.id == workspace_id)
                    .values(status=WorkspaceStatus.completed.value)
                )
                await s.commit()
        return await original_create_idempotent(
            self,
            workspace_id=workspace_id,
            operation_type=operation_type,
            status=status,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(OperationRepository, "create_idempotent", _race_create_idempotent)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=900,
    )
    state = _elapsed_initial_review_grace_state()

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
    assert sleep_fn.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id)
        recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
        assert len(recovery_operations) == 1
        assert recovery_operations[0].status == OperationStatus.cancelled.value
        ignored_events = [
            event for event in ws.events if event.event_type == "workspace.stale_callback_ignored"
        ]
        dispatch_events = [
            event for event in ws.events if event.event_type == "monitor.recovery_dispatched"
        ]
        assert ignored_events[-1].reason_code == "STALE_CALLBACK_IGNORED"
        assert ignored_events[-1].payload["callback_action"] == "recovery_dispatch"
        assert ignored_events[-1].payload["actual_status"] == WorkspaceStatus.completed.value
        assert dispatch_events == []
