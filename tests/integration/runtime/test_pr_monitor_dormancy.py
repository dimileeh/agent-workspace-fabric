"""Dormant human waits through the real monitor and transaction boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.db.enums import OperationStatus, OperationType
from awf.db.models import ResourceReservation, WorkspaceSecretLease
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    WorkerHeartbeatRepository,
    WorkspaceRepository,
)
from awf.node.compose_manager import ComposeManager, ComposeOperationError, ComposeTeardownResult
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    ShortCircuitCompleted,
    WaitForCI,
    decide,
)
from awf.runtime.pr_monitor_models import ActionsRunsState
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner, merge_loop
from awf.runtime.pr_monitor_runner import runner as runner_module
from awf.runtime.pr_monitor_runner.thread_disposition_gate import DispositionGateResult
from tests.integration.runtime._pr_monitor_runner_fixtures import _seed_monitoring_workspace
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient

pytest_plugins = ["tests.integration.runtime._pr_monitor_runner_fixtures"]


class PollContinuesError(Exception):
    """Stop the test when the runner schedules another ordinary poll."""


@pytest.fixture
async def wait(factory, cmd, adapter, tmp_path, monkeypatch, sleep_fn, request):
    now = datetime.now(UTC)
    ready = getattr(request, "param", None) == "ready"
    if ready:
        from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
            _seed_ready,
        )

        workspace_id = await _seed_ready(factory, create_task_attempt=True, create_worktree=False)
    else:
        workspace_id = await _seed_monitoring_workspace(
            factory, repo_url="https://github.com/example/repo.git"
        )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services:\n  agent:\n    image: example\n")
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.compose_file_path = str(compose_file)
        if not ready:
            workspace.monitor_claimed_by = "monitor"
            workspace.monitor_claim_expires_at = now + timedelta(hours=1)
        session.add(
            WorkspaceSecretLease(
                id="human-wait-lease",
                workspace_id=workspace_id,
                secret_name="example",
                kind="mount",
                target="/run/secrets/example",
                mode="ro",
                provider="protected",
                status="mounted",
                issued_at=now,
            )
        )
        session.add(
            ResourceReservation(
                id="human-wait-reservation",
                attempt_id=workspace.task_attempt.id,
                workspace_id=workspace_id,
                node_id="node",
                phase="monitoring_pr",
                steady_cpu=1,
                steady_memory_gb=1,
                peak_cpu=1,
                peak_memory_gb=1,
            )
        )

    status = PRStatus(42, "abc123", MergeableState.MERGEABLE, CheckState.SUCCESS, (), (), 0)
    forge = DefaultMergeMethodGitHubClient(cmd)
    forge.post_comment = AsyncMock()
    monkeypatch.setattr(runner_module, "observe_pr_head_sync", AsyncMock(return_value=status))
    monkeypatch.setattr(
        runner_module, "decide", lambda *_: NotifyHuman(message="Approval required")
    )
    containers = {"agent", "companion"}
    volumes = {"data"}
    compose_commands = []

    async def compose(_manager, _project, _file, args, **kwargs):
        compose_commands.append(args)
        if kwargs["operation"] == "down":
            containers.clear()
            if "-v" in args:
                volumes.clear()
        if kwargs["operation"] == "up":
            containers.add("agent")

    monkeypatch.setattr(ComposeManager, "_compose", compose)
    real_teardown = ComposeManager.teardown_project

    async def teardown_project(manager, **kwargs):
        return await teardown(manager, **kwargs)

    teardown = AsyncMock(side_effect=real_teardown)
    monkeypatch.setattr(ComposeManager, "teardown_project", teardown_project)

    class Monitor:
        current_time = now
        runtime_containers = containers
        runtime_volumes = volumes
        runtime_commands = compose_commands
        pr_status = status

        def build(self, threshold, *, sleep=sleep_fn):
            runner = PullRequestMonitorRunner(
                session_factory=factory,
                runner=cmd,
                gh=forge,
                adapter=adapter,
                worktrees_root=tmp_path / "git" / "worktrees",
                now=lambda: self.current_time,
                sleep=sleep,
                monitor_config=MonitorConfig(human_wait_dormant_after_seconds=threshold),
            )
            runner._fetch_status_for_decision = AsyncMock(return_value=self.pr_status)
            return runner

        async def run(self, runner, *, owner="monitor"):
            await runner.run(
                workspace_id=workspace_id,
                compose_project=f"awf_{workspace_id}",
                compose_file=compose_file,
                monitor_owner_id=owner,
            )

    return Monitor(), workspace_id, forge, teardown


async def test_human_wait_goes_dormant_at_threshold_without_reposting(factory, wait, sleep_fn):
    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time
    threshold = 42.25

    async def advance(seconds):
        assert len(sleep_fn.calls) < 2, "monitor slept again at the dormancy threshold"
        await sleep_fn(seconds)
        monitor.current_time = start + timedelta(
            seconds=threshold - 0.01 if len(sleep_fn.calls) == 1 else threshold
        )

    async with factory.begin() as session:
        (
            await WorkspaceRepository(session).get(workspace_id)
        ).human_wait_dormant_after_seconds = threshold
    await monitor.run(monitor.build(threshold, sleep=advance))

    assert len(sleep_fn.calls) == 2
    forge.post_comment.assert_awaited_once()
    teardown.assert_awaited_once()
    assert teardown.call_args.kwargs["remove_volumes"] is False
    assert monitor.runtime_containers == set()
    assert monitor.runtime_volumes == {"data"}
    assert monitor.runtime_commands == [["down", "--remove-orphans"]]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.status == "monitoring_pr"
        assert workspace.dormant_at == start + timedelta(seconds=threshold)
        assert workspace.awaiting_human_since == start
        assert workspace.awaiting_human_reason == "Approval required"
        assert (workspace.monitor_claimed_by, workspace.monitor_claim_expires_at) == (None, None)
        assert [
            event.reason_code
            for event in workspace.events
            if event.event_type == "workspace.dormant"
        ] == ["HUMAN_WAIT_DORMANT"]
        assert [
            op.result["outcome"] for op in workspace.operations if op.type == "human_wait"
        ].count("dormant") == 1
        assert all(op.status == "succeeded" for op in workspace.operations)
        lease = await session.get(WorkspaceSecretLease, "human-wait-lease")
        assert lease.status == "revoked"
        reservation = await session.scalar(select(ResourceReservation))
        assert reservation.released_at is None


@pytest.mark.parametrize(
    ("blocker", "threshold"),
    [
        ("missing_candidate", 30),
        ("failed_rebase", 30),
        ("missing_candidate", None),
        ("owned_paths", 30),
        ("owned_paths", None),
    ],
)
async def test_merge_gate_human_wait_honors_dormancy_opt_in(
    factory, wait, monkeypatch, blocker, threshold
):
    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time
    next_poll = start + timedelta(seconds=30)
    monitor.pr_status = replace(
        monitor.pr_status,
        merge_state_status=MergeStateStatus.CLEAN,
        actions_runs=ActionsRunsState.SETTLED,
    )
    if blocker == "owned_paths":
        monitor.pr_status = replace(monitor.pr_status, changed_paths=("src/outside.py",))
    monkeypatch.setattr(
        runner_module, "observe_pr_head_sync", AsyncMock(return_value=monitor.pr_status)
    )
    monkeypatch.setattr(runner_module, "decide", decide)

    async def advance(_seconds):
        if monitor.current_time == next_poll:
            raise PollContinuesError("merge-gate human wait scheduled another poll")
        monitor.current_time = next_poll

    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = threshold
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(
            workspace.task_attempt.id
        )
        assert candidate is not None
        if blocker == "failed_rebase":
            candidate.stale = True
            candidate.stale_reason = "STALE_TARGET_ADVANCED"
            await OperationRepository(session).create(
                workspace_id=workspace_id,
                operation_type=OperationType.rebase,
                status=OperationStatus.failed,
            )
            expected_reason = "Rebase conflicted. Manual intervention required."
        elif blocker == "owned_paths":
            workspace.owned_paths = ["src/owned.py"]
            workspace.task_policy = {"out_of_scope_changes": {"mode": "block"}}
            expected_reason = "OUT_OF_SCOPE_CHANGE"
        else:
            await session.delete(candidate)
            expected_reason = "candidate provenance is repaired"

    runner = monitor.build(threshold, sleep=advance)
    runner._config = replace(runner._config, auto_merge=True, initial_review_grace_period_seconds=0)
    if threshold is None:
        with pytest.raises(PollContinuesError):
            await monitor.run(runner)
        teardown.assert_not_awaited()
    else:
        await monitor.run(runner)
        teardown.assert_awaited_once()

    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.status == "monitoring_pr"
        assert workspace.awaiting_human_since == start
        assert expected_reason in workspace.awaiting_human_reason
        if threshold is None:
            assert workspace.dormant_at is None
            assert workspace.monitor_claimed_by == "monitor"
        else:
            assert workspace.dormant_at == next_poll
            assert workspace.monitor_claimed_by is None
            assert [
                op.result["outcome"] for op in workspace.operations if op.type == "human_wait"
            ].count("dormant") == 1


@pytest.mark.parametrize(
    ("blocker", "expected_reason"),
    [
        ("disposition_before_lock", "Review thread disposition is missing"),
        ("fresh_review", "merge-blocking changes-requested review"),
        ("disposition_after_lock", "Review thread disposition is missing"),
        ("merge_method", "MERGE_METHOD_MISMATCH"),
    ],
)
async def test_nested_merge_human_wait_persists_notice_before_sleep(
    factory, wait, monkeypatch, blocker, expected_reason
):
    monitor, workspace_id, forge, _ = wait
    monitor.pr_status = replace(
        monitor.pr_status,
        merge_state_status=MergeStateStatus.CLEAN,
        actions_runs=ActionsRunsState.SETTLED,
    )
    monkeypatch.setattr(
        runner_module, "observe_pr_head_sync", AsyncMock(return_value=monitor.pr_status)
    )
    monkeypatch.setattr(runner_module, "decide", decide)
    runner = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    runner._config = replace(
        runner._config,
        auto_merge=True,
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=0,
        pre_merge_settle_seconds=0,
    )

    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
    if blocker == "fresh_review":
        review = ReviewComment("review-1", "Changes requested", blocks_merge=True)
        runner._fetch_status_for_decision.side_effect = [
            monitor.pr_status,
            replace(monitor.pr_status, blocking_reviews=(review,)),
        ]
    elif blocker == "merge_method":
        monkeypatch.setattr(forge, "fetch_repo_merge_methods", AsyncMock(return_value=()))
    else:
        gate_name = (
            "review_thread_disposition_gate_with_reopen"
            if blocker == "disposition_before_lock"
            else "review_thread_disposition_gate"
        )
        gate = DispositionGateResult(blocker=expected_reason)
        monkeypatch.setattr(merge_loop, gate_name, AsyncMock(return_value=gate))

    with pytest.raises(PollContinuesError):
        await monitor.run(runner)

    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert expected_reason in workspace.awaiting_human_reason
        assert "notified" in (workspace.monitor_threads_addressed or {}).values()


async def test_expiration_scan_cannot_overwrite_dormancy_revocation(factory, wait, monkeypatch):
    from awf.db.repositories import SecretLeaseRepository
    from awf.hosted_node.lifecycle_store import database_quiescent, quiescence_blockers

    monitor, workspace_id, _, _ = wait
    now = monitor.current_time
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = now - timedelta(minutes=2)
        lease = await session.get(WorkspaceSecretLease, "human-wait-lease")
        lease.expires_at = now

    runner = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    async with factory.begin() as expiring_session:
        execute = expiring_session.execute

        async def commit_dormancy_after_selection(statement, *args, **kwargs):
            assert statement.is_select
            result = await execute(statement, *args, **kwargs)
            monkeypatch.setattr(expiring_session, "execute", execute)
            await monitor.run(runner)
            return result

        monkeypatch.setattr(expiring_session, "execute", commit_dormancy_after_selection)
        expired = await SecretLeaseRepository(expiring_session).expire_due_leases(now=now)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        lease = await session.get(WorkspaceSecretLease, "human-wait-lease")
        assert workspace.dormant_at == now
        assert lease.status == "revoked"
        assert lease.revoked_at == now
        assert expired == []
        assert [
            event.reason_code
            for event in workspace.events
            if event.event_type == "workspace.secret_lease"
        ] == ["SECRET_LEASE_REVOKED"]
        assert await quiescence_blockers(session) == []
        assert await database_quiescent(session)
    assert monitor.runtime_containers == set()


@pytest.mark.parametrize(
    "action", [NotifyHuman(message="Old escalation"), WaitForCI(), ShortCircuitCompleted()]
)
async def test_stale_poll_cannot_begin_an_operation_after_dormancy(
    factory, wait, monkeypatch, action
):
    from awf.hosted_node.lifecycle_store import database_quiescent, quiescence_blockers

    monitor, workspace_id, forge, teardown = wait
    now = monitor.current_time
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = now - timedelta(minutes=2)

    stale_status = replace(monitor.pr_status, head_sha="stale-head")
    monkeypatch.setattr(
        runner_module,
        "observe_pr_head_sync",
        AsyncMock(side_effect=lambda _runner, *, status, **_kwargs: status),
    )
    monkeypatch.setattr(
        runner_module,
        "decide",
        lambda status, *_: (
            action
            if status.head_sha == stale_status.head_sha
            else NotifyHuman(message="Approval required")
        ),
    )
    old_poll_at_forge = asyncio.Event()
    resume_old_poll = asyncio.Event()
    old_sleep = AsyncMock(side_effect=PollContinuesError)
    stale_runner = monitor.build(30, sleep=old_sleep)

    async def return_stale_status(**_kwargs):
        old_poll_at_forge.set()
        await resume_old_poll.wait()
        return stale_status

    stale_runner._fetch_status_for_decision = return_stale_status
    stale_poll = asyncio.create_task(monitor.run(stale_runner))
    try:
        await asyncio.wait_for(old_poll_at_forge.wait(), timeout=10)
        async with factory.begin() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            workspace.monitor_claim_expires_at = now - timedelta(seconds=1)
            await session.flush()
            assert await repo.claim_monitoring_pr(
                workspace_id, owner_id="replacement", lease_expires_at=now + timedelta(hours=1)
            )

        await monitor.run(
            monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)),
            owner="replacement",
        )
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace.dormant_at == now
            operation_ids = {op.id for op in workspace.operations}
            assert operation_ids
            assert await database_quiescent(session)
        forge.post_comment.assert_awaited_once()
        resume_old_poll.set()
        outcome = await asyncio.gather(stale_poll, return_exceptions=True)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert {op.id for op in workspace.operations} == operation_ids
            assert workspace.dormant_at == now
            assert workspace.monitor_claimed_by is None
            assert await quiescence_blockers(session) == []
            assert await database_quiescent(session)
        forge.post_comment.assert_awaited_once()
        old_sleep.assert_not_awaited()
        assert outcome == [None]
        teardown.assert_awaited_once()
        assert monitor.runtime_containers == set()
    finally:
        if not stale_poll.done():
            stale_poll.cancel()
            await asyncio.gather(stale_poll, return_exceptions=True)


async def test_stale_human_wait_stops_before_notification_when_attention_is_rejected(
    factory, wait, monkeypatch
):
    monitor, workspace_id, forge, teardown = wait
    now = monitor.current_time
    start = now - timedelta(minutes=2)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = start
    monkeypatch.setattr(
        runner_module,
        "decide",
        lambda _status, _state, config: NotifyHuman(
            message="Old escalation"
            if config.human_wait_dormant_after_seconds is None
            else "New escalation"
        ),
    )
    stale_runner = monitor.build(None, sleep=AsyncMock(side_effect=PollContinuesError))
    set_attention = stale_runner._set_workspace_attention

    async def take_over_then_set_attention(*args, **kwargs):
        async with factory.begin() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            (operation,) = workspace.operations
            assert operation.type == "human_wait" and operation.status == "running"
            workspace.monitor_claim_expires_at = now - timedelta(seconds=1)
            await session.flush()
            assert await repo.claim_monitoring_pr(
                workspace_id,
                owner_id="replacement",
                lease_expires_at=now + timedelta(hours=1),
            )
        await monitor.run(
            monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)),
            owner="replacement",
        )
        return await set_attention(*args, **kwargs)

    monkeypatch.setattr(stale_runner, "_set_workspace_attention", take_over_then_set_attention)
    await monitor.run(stale_runner)

    forge.post_comment.assert_awaited_once()
    stale_runner._deps.sleep.assert_not_awaited()
    teardown.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.awaiting_human_since == start
        assert workspace.awaiting_human_reason == "New escalation"
        assert workspace.monitor_claimed_by is None
        assert workspace.dormant_at == now
        assert {op.result["outcome"]: op.status for op in workspace.operations} == {
            "superseded": "cancelled",
            "dormant": "succeeded",
        }


async def test_expired_monitor_claim_cannot_start_an_opted_in_human_wait(factory, wait):
    monitor, workspace_id, forge, teardown = wait
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.monitor_claim_expires_at = monitor.current_time - timedelta(seconds=1)

    sleep = AsyncMock(side_effect=PollContinuesError)
    await monitor.run(monitor.build(30, sleep=sleep))

    forge.post_comment.assert_not_awaited()
    sleep.assert_not_awaited()
    teardown.assert_not_awaited()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.operations == []
        assert workspace.awaiting_human_since is None
        assert workspace.dormant_at is None


async def test_restart_settles_interrupted_waits_after_head_changes(factory, wait, monkeypatch):
    monitor, workspace_id, _, teardown = wait
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30

    # A cancellation during a passive sleep leaves its operation open.
    interrupted = AsyncMock(side_effect=asyncio.CancelledError)
    with monkeypatch.context() as ci_poll:
        ci_poll.setattr(runner_module, "decide", lambda *_: WaitForCI())
        with pytest.raises(asyncio.CancelledError):
            await monitor.run(monitor.build(30, sleep=interrupted))
    with pytest.raises(asyncio.CancelledError):
        await monitor.run(monitor.build(30, sleep=interrupted))

    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        interrupted_ids = {op.type: op.id for op in workspace.operations if op.status == "running"}
        assert set(interrupted_ids) == {"monitor_state", "human_wait"}
        active_merge = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.monitor_state,
            status=OperationStatus.running,
            payload={"owner": "pr_monitor", "action": "merge"},
        )
        active_merge_id = active_merge.id
    monitor.current_time += timedelta(seconds=31)
    monitor.pr_status = replace(monitor.pr_status, head_sha="new-head")
    monkeypatch.setattr(
        runner_module, "observe_pr_head_sync", AsyncMock(return_value=monitor.pr_status)
    )

    with pytest.raises(PollContinuesError):
        await monitor.run(monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)))
    teardown.assert_not_awaited()
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.dormant_at is None
        operations = OperationRepository(session)
        active_merge = await operations.get(active_merge_id)
        assert active_merge.status == "running"
        await operations.finish(active_merge, status=OperationStatus.succeeded)

    await monitor.run(monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)))
    teardown.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.dormant_at == monitor.current_time
        by_id = {op.id: op for op in workspace.operations}
        for operation_id in interrupted_ids.values():
            assert by_id[operation_id].status == "cancelled"
            assert by_id[operation_id].result["outcome"] == "superseded"
        assert by_id[active_merge_id].status == "succeeded"
        (current,) = [
            op for op in workspace.operations if (op.result or {}).get("outcome") == "dormant"
        ]
        assert current.payload["source_head_sha"] == "new-head"
        assert all(op.status not in {"pending", "running"} for op in workspace.operations)


@pytest.mark.parametrize("action", ["unconfigured", "ci", "queued"])
async def test_wait_paths_that_cannot_go_dormant(factory, wait, monkeypatch, action):
    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time - timedelta(hours=1)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.awaiting_human_since = start
        workspace.awaiting_human_reason = "Existing wait"
        threshold = None if action == "unconfigured" else 30
        workspace.human_wait_dormant_after_seconds = threshold

    runner = monitor.build(threshold, sleep=AsyncMock(side_effect=PollContinuesError))
    if action == "ci":
        monkeypatch.setattr(runner_module, "decide", lambda *_: WaitForCI())
    if action == "queued":
        from awf.runtime.pr_monitor_runner import notify_human_loop

        monkeypatch.setattr(notify_human_loop, "_is_manual_ready_handoff", lambda *_: True)
        monkeypatch.setattr(runner_module, "decide", lambda *_: NotifyHuman())
        runner._refresh_scope_policy_for_merge = AsyncMock(return_value=False)
        runner._merge_queue_blockers_for_workspace = AsyncMock(return_value=[object()])
        runner._wait_for_merge_queue = AsyncMock(side_effect=PollContinuesError)

    with pytest.raises(PollContinuesError):
        await monitor.run(runner)
    teardown.assert_not_awaited()
    if action == "unconfigured":
        forge.post_comment.assert_awaited_once()
    else:
        forge.post_comment.assert_not_awaited()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.dormant_at is None
        assert workspace.monitor_claimed_by == "monitor"
        assert workspace.awaiting_human_since == (start if action == "unconfigured" else None)


@pytest.mark.parametrize("owner", ["hosted", "changed", "expired", "new_episode"])
async def test_existing_human_episode_and_ownership_on_resume(factory, wait, monkeypatch, owner):
    from awf.runtime.pr_monitor_runner import notify_human_loop

    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time - timedelta(minutes=2)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = start
        if owner == "hosted":
            workspace.task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}

    if owner != "hosted":
        suspend = notify_human_loop.suspend_human_wait

        async def change_wait_before_suspension(*args, **kwargs):
            async with factory.begin() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                if owner == "changed":
                    workspace.monitor_claimed_by = "successor"
                elif owner == "expired":
                    workspace.monitor_claim_expires_at = start
                else:
                    workspace.awaiting_human_since = monitor.current_time
            return await suspend(*args, **kwargs)

        monkeypatch.setattr(notify_human_loop, "suspend_human_wait", change_wait_before_suspension)

    runner = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    if owner == "new_episode":
        with pytest.raises(PollContinuesError):
            await monitor.run(runner)
    else:
        await monitor.run(runner)
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if owner == "hosted":
            assert workspace.dormant_at == monitor.current_time
            assert workspace.monitor_claimed_by is None
            assert workspace.execution_claimed_by is None
            assert workspace.execution_claim_expires_at is None
            assert workspace.execution_claim_epoch == 0
        else:
            assert workspace.dormant_at is None
            assert workspace.monitor_claimed_by == (
                "successor" if owner == "changed" else "monitor"
            )
            assert any(
                event.event_type == "workspace.dormancy_failed" for event in workspace.events
            )
    teardown.assert_not_awaited()


@pytest.mark.parametrize("wait", ["ready"], indirect=True)
@pytest.mark.parametrize("threshold", [30, None], ids=["opted-in", "default"])
async def test_inline_executor_handoff_preserves_execution_claim(
    factory, wait, cmd, monkeypatch, tmp_path, threshold
):
    from awf.runtime.pr_creator import PullRequestResult
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
        _make_executor,
        _queue_pre_agent_symlink_baseline,
    )

    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time
    expires_at = start + timedelta(hours=1)
    owner = "executor"
    epoch = 7
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = threshold
        workspace.execution_claim_epoch = epoch
        workspace.test_commands = []
    (tmp_path / "work" / "worktrees" / workspace_id).mkdir(parents=True)

    _queue_pre_agent_symlink_baseline(cmd)
    cmd.queue_result(stdout="adapter ok")
    cmd.queue_result(stdout="awf/x\n")  # current branch
    cmd.queue_result()  # git add
    cmd.queue_result(stdout="a.py\n")  # staged changes
    cmd.queue_result()  # git commit
    cmd.queue_result(stdout="1\n")  # commit count
    cmd.queue_result()  # untracked files
    cmd.queue_result(stdout="abc123\n")  # validated head
    cmd.queue_result(stdout="a.py\n")  # committed diff paths
    cmd.queue_result(stdout="M\0a.py\0")  # committed diff status

    async def advance(_seconds):
        if threshold is None:
            raise PollContinuesError
        assert monitor.current_time == start, "monitor slept after the dormancy threshold"
        monitor.current_time = start + timedelta(seconds=30)

    sleep = AsyncMock(side_effect=advance)
    runner = monitor.build(threshold, sleep=sleep)
    run = runner.run

    async def run_with_live_claim(**kwargs):
        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            persisted_claim = (row.execution_claimed_by, row.execution_claim_epoch)
            assert persisted_claim == (owner, epoch)
            assert kwargs.get("dormancy_execution_claim") == (
                persisted_claim if threshold is not None else None
            )
            assert row.execution_claim_expires_at == expires_at
        if threshold is None:
            with pytest.raises(PollContinuesError):
                await run(**kwargs)
        else:
            await run(**kwargs)

    run_spy = AsyncMock(side_effect=run_with_live_claim)
    monkeypatch.setattr(runner, "run", run_spy)
    pr_creator = AsyncMock()
    pr_creator.push_and_open.return_value = PullRequestResult(
        url="https://github.com/x/y/pull/42", branch="awf/x", head_sha="abc123"
    )
    executor = _make_executor(
        cmd,
        factory,
        tmp_path,
        pr_creator=pr_creator,
        pr_monitor_factory=lambda *_args, **_kwargs: runner,
    )

    await executor.execute(
        workspace_id, execution_owner_id=owner, execution_lease_expires_at=expires_at
    )

    run_spy.assert_awaited_once()
    sleep.assert_awaited_once()
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "monitoring_pr"
        if threshold is None:
            teardown.assert_not_awaited()
            assert row.dormant_at is None
            assert (row.execution_claimed_by, row.execution_claim_expires_at) == (owner, expires_at)
        else:
            teardown.assert_awaited_once()
            assert row.dormant_at == start + timedelta(seconds=threshold)
            assert (row.execution_claimed_by, row.execution_claim_expires_at) == (None, None)
        assert row.execution_claim_epoch == epoch


@pytest.mark.parametrize(
    "stage",
    [
        "compose_teardown",
        "operation_finish",
        "operation_repository_finish",
        "secret_revocation",
        "event",
        "monitor_log",
        "commit",
        "ambiguous_commit",
    ],
)
async def test_dormancy_failures_preserve_a_working_wait(factory, wait, monkeypatch, stage):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from awf.runtime.pr_monitor_runner import dormancy
    from awf.service.secret_leases import SecretLeaseService

    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time - timedelta(hours=1)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.awaiting_human_since = start
        workspace.human_wait_dormant_after_seconds = 30

    async def fail(*args, **kwargs):
        raise SQLAlchemyError("synthetic database error")

    sink = AsyncMock(stream_id="monitor.log")
    runner = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    runner._open_monitor_log = AsyncMock(return_value=sink)
    if stage == "compose_teardown":

        async def partial_teardown(*args, **kwargs):
            monitor.runtime_containers.discard("agent")
            return ComposeTeardownResult("failed", "DOCKER_COMPOSE_DOWN_FAILED")

        teardown.side_effect = partial_teardown
    elif stage == "operation_finish":
        monkeypatch.setattr(dormancy, "finish_monitor_operation", fail)
    elif stage == "operation_repository_finish":
        finish = OperationRepository.finish
        attempted_statuses = []

        async def fail_operation_write(operations, operation, *, status, **kwargs):
            attempted_statuses.append(status)
            # Flush the mutation before raising so the regression requires
            # rolling it back while still committing the failure event.
            await finish(operations, operation, status=status, **kwargs)
            operation.status = None
            await operations._session.flush()

        monkeypatch.setattr(OperationRepository, "finish", fail_operation_write)
    elif stage == "secret_revocation":
        revoke = SecretLeaseService.revoke_workspace_secret_leases

        async def fail_after_revoke(*args, **kwargs):
            await revoke(*args, **kwargs)
            await fail()

        monkeypatch.setattr(SecretLeaseService, "revoke_workspace_secret_leases", fail_after_revoke)
    elif stage == "event":
        add_event = WorkspaceRepository.add_event

        async def fail_after_event(self, workspace, **kwargs):
            result = await add_event(self, workspace, **kwargs)
            if kwargs["event_type"] == "workspace.dormant":
                await fail()
            return result

        monkeypatch.setattr(WorkspaceRepository, "add_event", fail_after_event)
    elif stage == "monitor_log":

        async def fail_requested_log(line):
            if "monitor.dormancy_requested" in line:
                raise OSError("synthetic log failure")

        sink.write.side_effect = fail_requested_log
    else:
        commit = AsyncSession.commit

        async def fail_commit(session):
            dormant_write = any(
                getattr(obj, "dormant_at", None) is not None for obj in session.dirty
            )
            if dormant_write and stage == "commit":
                await fail()
            await commit(session)
            if dormant_write:
                await fail()

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)

    if stage == "monitor_log":
        with pytest.raises(PollContinuesError):
            await monitor.run(runner)
        teardown.assert_not_awaited()
    else:
        await monitor.run(runner)
        teardown.assert_awaited_once()
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        lease = await session.get(WorkspaceSecretLease, "human-wait-lease")
        assert workspace.status == "monitoring_pr"
        assert workspace.awaiting_human_since == start
        if stage == "ambiguous_commit":
            assert workspace.dormant_at == monitor.current_time
            assert workspace.monitor_claimed_by is None and lease.status == "revoked"
            assert not any(
                event.event_type == "workspace.dormancy_failed" for event in workspace.events
            )
            return

        assert workspace.dormant_at is None
        assert workspace.monitor_claimed_by == "monitor" and lease.status == "mounted"
        assert not any(event.event_type == "workspace.dormant" for event in workspace.events)
        assert not any(
            (operation.result or {}).get("outcome") == "dormant"
            for operation in workspace.operations
        )
        (event,) = [
            event for event in workspace.events if event.event_type == "workspace.dormancy_failed"
        ]
        assert event.reason_code == "HUMAN_WAIT_DORMANCY_FAILED"
        if stage == "operation_repository_finish":
            assert event.payload["stage"] == "operation_finish"
            assert event.payload["failure_code"] == "IntegrityError"
            assert attempted_statuses == [OperationStatus.succeeded, OperationStatus.failed]
            (active_wait,) = workspace.operations
            assert active_wait.type == "human_wait" and active_wait.status == "running"
            assert active_wait.finished_at is None and active_wait.result is None
        else:
            assert event.payload["stage"] == stage
        assert event.payload["runtime_recovery_required"] == (stage != "monitor_log")
    assert any("monitor.dormancy_failed" in call.args[0] for call in sink.write.await_args_list)
    if stage == "operation_repository_finish":
        monkeypatch.setattr(OperationRepository, "finish", finish)
        await monitor.run(monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)))
        forge.post_comment.assert_awaited_once()
        async with factory() as session:
            row = await WorkspaceRepository(session).get(workspace_id)
            assert row.dormant_at == monitor.current_time
            (settled_wait,) = row.operations
            assert settled_wait.id == active_wait.id
            assert settled_wait.result["outcome"] == "dormant"


async def test_dormant_monitor_is_excluded_from_scheduling_and_stale_recovery(
    factory, wait, cmd, tmp_path
):
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.control.worker.claims import (
        _monitor_recovery_terminal_finalize_status,
        _workspace_is_monitoring_pr,
    )
    from awf.runtime.inspection import RuntimeSnapshot
    from awf.service.metrics_slo import _count_monitor_completions
    from awf.service.workspace_runtime_health import (
        WorkspaceRuntimeFinding,
        classify_runtime_snapshot,
        runtime_workspace_from_workspace,
        runtime_workspaces_from_session,
    )
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
        _make_executor,
    )

    monitor, workspace_id, _, _ = wait
    other = await _seed_monitoring_workspace(
        factory, repo_url="https://github.com/example/other.git"
    )
    now = monitor.current_time
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        waiting = await repo.get(other)
        workspace.monitor_claim_expires_at = now - timedelta(minutes=1)
        for row in (workspace, waiting):
            row.node_id = "node"
            row.created_at = now - timedelta(hours=1)

    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=AsyncMock(),
        config=WorkerConfig(node_id="node"),
    )
    initial = await worker._list_stale_active_execution_candidates(exclude_ids=set())
    candidate = next(row for row in initial if row.workspace_id == workspace_id)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.dormant_at = now

    assert await worker._list_monitoring_pr(limit=10) == [other]
    assert [
        row.workspace_id
        for row in await worker._list_stale_active_execution_candidates(exclude_ids=set())
    ] == [other]
    assert not await worker._stale_active_candidate_is_current(candidate)
    assert not await worker._claim_monitoring_pr(workspace_id)
    worker._worker_id = "monitor"
    assert not await worker._refresh_monitoring_pr_claim(workspace_id)
    assert not await _workspace_is_monitoring_pr(worker, workspace_id)
    assert await _monitor_recovery_terminal_finalize_status(
        worker, workspace_id, after_successful_handoff=True
    ) == (OperationStatus.succeeded, None, None)
    await worker._record_active_salvage_monitor_resume_cooldown(
        workspace_id,
        recovery_operation_id="op_previous",
        cooldown_until=now + timedelta(minutes=1),
    )
    executor = _make_executor(cmd, factory, tmp_path)
    assert await executor.resume_pr_monitor_handoff(workspace_id) is None
    assert not await executor.verify_resume_monitor_start(workspace_id)
    await worker._record_recoverable_runtime_stranding(
        candidate,
        RuntimeSnapshot(stack_state="missing"),
        WorkspaceRuntimeFinding(
            workspace_id=workspace_id,
            workspace_status="monitoring_pr",
            status="stranded",
            reason_code="AGENT_CONTAINER_MISSING",
            decision="remonitor_workspace",
            message="Missing agent",
        ),
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.monitor_claimed_by == "monitor"
        assert not any(
            event.event_type
            in (
                "workspace.runtime_stranded_detected",
                "workspace.active_execution_salvage_monitor_resume_cooldown",
            )
            for event in workspace.events
        )
        assert [row.workspace_id for row in await runtime_workspaces_from_session(session)] == [
            other
        ]
        assert (
            classify_runtime_snapshot(
                runtime_workspace_from_workspace(workspace), RuntimeSnapshot(stack_state="missing")
            )
            is None
        )
        _, _, stuck = await _count_monitor_completions(
            session, window_start=now - timedelta(days=1), sla_seconds=60, now=now
        )
        assert stuck == 1

    # The deferred scan must also ignore a dormant row carrying a stale,
    # inconsistent execution record while retaining the ordinary deferred row.
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        for row_id in (workspace_id, other):
            row = await repo.get(row_id)
            row.execution_claimed_by = "other-executor"
            row.execution_claim_expires_at = now + timedelta(minutes=1)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="other-executor",
            node_id="node",
            started_at=now - timedelta(minutes=1),
            last_heartbeat_at=now,
            poll_interval_seconds=1,
        )
    async with factory() as session:
        deferred = await WorkspaceRepository(
            session
        ).list_monitoring_pr_deferred_active_execution_claim_workspaces(
            limit=10, claim_cutoff=now, owner_id="monitor", node_id="node"
        )
        assert [row.id for row in deferred] == [other]


@pytest.mark.parametrize("suspend_at", ["inspection", "cleanup", None])
async def test_stale_runtime_fallback_preserves_concurrent_dormancy(
    factory, wait, monkeypatch, suspend_at
):
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.node.cleanup import WorkspaceCleanupResult
    from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

    monitor, workspace_id, _, teardown = wait
    now = monitor.current_time
    stale_event = "workspace.stale_active_execution_detected"
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        workspace.node_id = "node"
        workspace.awaiting_human_since = now - timedelta(hours=1)
        workspace.monitor_claimed_by = None
        workspace.monitor_claim_expires_at = None
        workspace.human_wait_dormant_after_seconds = 30 if suspend_at else None
        await repo.add_event(
            workspace, event_type=stale_event, reason_code="STALE_ACTIVE_EXECUTION"
        )

    snapshot = RuntimeSnapshot(
        stack_state="running", services=[RuntimeService("agent", "agent", None, "running")]
    )
    cleaner = AsyncMock()
    cleaner.cleanup.return_value = WorkspaceCleanupResult.from_steps([])
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=AsyncMock(),
        config=WorkerConfig(node_id="node"),
        runtime_cleaner=cleaner,
        runtime_inspector=AsyncMock(),
    )
    (candidate,) = await worker._list_stale_active_execution_candidates(exclude_ids=set())

    async def suspend():
        async with factory.begin() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            workspace.monitor_claimed_by = "monitor"
            workspace.monitor_claim_expires_at = now + timedelta(hours=1)
        await monitor.run(monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)))

    async def inspect(_project):
        if suspend_at == "inspection":
            await suspend()
        return snapshot

    worker._runtime_inspector.inspect.side_effect = inspect
    if suspend_at == "cleanup":
        can_fail = worker._stale_active_execution_can_fail

        async def suspend_before_cleanup(candidate):
            assert await can_fail(candidate)
            await suspend()
            return True

        monkeypatch.setattr(worker, "_stale_active_execution_can_fail", suspend_before_cleanup)

    await worker._recover_stale_active_execution(candidate)
    if suspend_at is None:
        cleaner.cleanup.assert_awaited_once()
        assert cleaner.cleanup.call_args.kwargs["remove_volumes"] is True
        teardown.assert_not_awaited()
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace.status == "failed"
            assert workspace.dormant_at is None
        return

    # Independently exercise the guarded write with the same obsolete candidate.
    await worker._fail_stale_active_execution(candidate, snapshot)
    cleaner.cleanup.assert_not_awaited()
    teardown.assert_awaited_once()
    assert monitor.runtime_volumes == {"data"}
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.status == "monitoring_pr"
        assert workspace.dormant_at == now
        assert workspace.monitor_claimed_by is None
        assert [event.event_type for event in workspace.events].count(stale_event) == 1


@pytest.mark.parametrize("keep_execution_claim", [False, True], ids=["monitor-only", "dual-claim"])
async def test_worker_finalizes_after_monitor_goes_dormant(
    factory, wait, cmd, monkeypatch, tmp_path, keep_execution_claim
):
    from awf.control.executor import monitor_handoff, monitor_resume_runtime, runtime_setup_replay
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.runtime.inspection import RuntimeSnapshot
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
        _make_executor,
    )

    monitor, workspace_id, forge, teardown = wait
    now = monitor.current_time
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.awaiting_human_since = now - timedelta(hours=1)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.node_id = "node"
        workspace.monitor_claimed_by = workspace.monitor_claim_expires_at = None

    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        runtime_setup_replay, "capture_agent_container_identity", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        monitor_handoff, "replay_setup_on_monitor_resume", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        monitor_resume_runtime,
        "inspect_resume_runtime",
        AsyncMock(return_value=RuntimeSnapshot(stack_state="running")),
    )
    executor = _make_executor(
        cmd,
        factory,
        tmp_path,
        compose=ComposeManager(work_dir=tmp_path, template_path=tmp_path / "unused"),
        pr_monitor_factory=lambda *_args, **_kwargs: monitor.build(
            30, sleep=AsyncMock(side_effect=PollContinuesError)
        ),
    )
    cleaner = AsyncMock()
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=executor,
        config=WorkerConfig(node_id="node"),
        runtime_cleaner=cleaner,
    )
    execution_owner = worker._worker_id if keep_execution_claim else None
    execution_expiry = now + timedelta(hours=1) if keep_execution_claim else None
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        row.execution_claimed_by = execution_owner
        row.execution_claim_expires_at = execution_expiry
        execution_epoch = row.execution_claim_epoch

    if execution_owner is not None:
        # Claim recovery after the inline poll has already loaded the workspace.
        async def claim_during_status_fetch(**_kwargs):
            assert await worker._claim_monitoring_pr(workspace_id)
            return monitor.pr_status

        inline_sleep = AsyncMock(side_effect=PollContinuesError)
        inline_runner = monitor.build(30, sleep=inline_sleep)
        inline_runner._fetch_status_for_decision = claim_during_status_fetch
        await inline_runner.run(
            workspace_id=workspace_id,
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            dormancy_execution_claim=(execution_owner, execution_epoch),
        )
        inline_sleep.assert_not_awaited()
        forge.post_comment.assert_not_awaited()
        teardown.assert_not_awaited()
    else:
        assert await worker._claim_monitoring_pr(workspace_id)

    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.monitor_claimed_by == worker._worker_id
        assert (row.execution_claimed_by, row.execution_claim_expires_at) == (
            execution_owner,
            execution_expiry,
        )
        assert all(op.type != OperationType.human_wait.value for op in row.operations)
    recovery_id = worker._monitor_recovery_operation_ids[workspace_id]
    release_runtime = AsyncMock(wraps=worker._release_terminal_runtime_promptly)
    monkeypatch.setattr(worker, "_release_terminal_runtime_promptly", release_runtime)
    await worker._safely_resume_claimed_pr_monitor(workspace_id, recovery_operation_id=recovery_id)

    release_runtime.assert_awaited_once_with(workspace_id)
    cleaner.cleanup.assert_not_awaited()
    teardown.assert_awaited_once()
    forge.post_comment.assert_awaited_once()
    assert workspace_id not in worker._monitor_recovery_operation_ids
    assert monitor.runtime_containers == set()
    assert monitor.runtime_volumes == {"data"}
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "monitoring_pr"
        assert row.dormant_at == now
        assert (row.monitor_claimed_by, row.monitor_claim_expires_at) == (None, None)
        assert (row.execution_claimed_by, row.execution_claim_expires_at) == (None, None)
        recovery = await OperationRepository(session).get(recovery_id)
        assert recovery.status == "succeeded"
        assert all(op.status == "succeeded" for op in row.operations)


async def test_failed_teardown_is_followed_by_worker_compose_recovery_before_work(
    factory, wait, cmd, monkeypatch, tmp_path
):
    from awf.control.executor import (
        monitor_handoff,
        monitor_resume_restart_escalation,
        monitor_resume_runtime,
        runtime_setup_replay,
    )
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
    from awf.runtime.pr_monitor import SyncBase
    from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
        _make_executor,
    )

    monitor, workspace_id, forge, teardown = wait
    now = monitor.current_time
    runner = monitor.build(30)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.awaiting_human_since = now - timedelta(hours=1)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.node_id = "node"

    async def partial_down(*args, **kwargs):
        monitor.runtime_containers.discard("agent")
        return ComposeTeardownResult("failed", "DOCKER_COMPOSE_DOWN_FAILED")

    teardown.side_effect = partial_down
    await monitor.run(runner)
    assert "agent" not in monitor.runtime_containers

    resumed = monitor.build(30)
    calls = []

    async def work(**kwargs):
        assert "agent" in monitor.runtime_containers
        assert isinstance(kwargs["action"], SyncBase)
        calls.append("sync_base")
        return True

    monkeypatch.setattr(resumed, "_execute", work)
    monkeypatch.setattr(runner_module, "decide", lambda *_: SyncBase())
    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        runtime_setup_replay, "capture_agent_container_identity", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        monitor_handoff, "replay_setup_on_monitor_resume", AsyncMock(return_value=True)
    )

    async def inspect(_project):
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(name, name, None, "running") for name in monitor.runtime_containers
            ],
        )

    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", inspect)
    backoff = AsyncMock()
    monkeypatch.setattr(monitor_resume_restart_escalation, "_sleep", backoff)
    compose = ComposeManager(work_dir=tmp_path, template_path=tmp_path / "unused")
    compose_command = compose._compose
    restart_attempts = []

    async def up_fails_once(*args, **kwargs):
        if kwargs["operation"] == "up" and args[2][-1] == "agent":
            restart_attempts.append(args[2])
            if len(restart_attempts) == 1:
                raise ComposeOperationError(
                    operation="up",
                    returncode=1,
                    stdout="",
                    stderr="synthetic restart failure",
                    reason_code="DOCKER_COMPOSE_UP_FAILED",
                )
        return await compose_command(*args, **kwargs)

    monkeypatch.setattr(compose, "_compose", up_fails_once)
    executor = _make_executor(
        cmd,
        factory,
        tmp_path,
        compose=compose,
        pr_monitor_factory=lambda *_args, **_kwargs: resumed,
    )
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=executor,
        config=WorkerConfig(node_id="node"),
    )
    worker._worker_id = "monitor"
    await worker._release_monitoring_pr_claim(workspace_id)
    assert await worker._claim_monitoring_pr(workspace_id)
    await worker._safely_resume_claimed_pr_monitor(
        workspace_id, recovery_operation_id=worker._monitor_recovery_operation_ids[workspace_id]
    )
    assert calls == []
    assert len(restart_attempts) == 1
    assert "agent" not in monitor.runtime_containers
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "monitoring_pr"
        assert row.dormant_at is None and row.monitor_claimed_by is None
        (restart_failed,) = [
            event
            for event in row.events
            if event.event_type == "workspace.monitor_runtime_restart_failed"
        ]
        assert restart_failed.reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
        assert restart_failed.payload["reason_code"] == "DOCKER_COMPOSE_UP_FAILED"

    assert await worker._claim_monitoring_pr(workspace_id)
    await worker._safely_resume_claimed_pr_monitor(
        workspace_id, recovery_operation_id=worker._monitor_recovery_operation_ids[workspace_id]
    )
    assert calls == ["sync_base"]
    assert len(restart_attempts) == 2
    backoff.assert_awaited_once()
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.dormant_at is None
        assert row.monitor_claimed_by is None
        assert row.status == "monitoring_pr"


async def test_notice_survives_restart_during_wait_and_logged_dormancy(factory, wait, tmp_path):
    from awf.runtime.logs import LogStore

    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30

    async def slow_notice(**kwargs):
        # A slow first notification must still finish the first poll as a wait.
        monitor.current_time += timedelta(seconds=31)

    forge.post_comment.side_effect = slow_notice
    with pytest.raises(PollContinuesError):
        await monitor.run(monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError)))
    teardown.assert_not_awaited()

    resumed = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    log_root = tmp_path / "logs"
    resumed._deps.log_store = LogStore(root=log_root, session_factory=factory)
    await monitor.run(resumed)
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.awaiting_human_since == start
        assert row.dormant_at == start + timedelta(seconds=31)
    lines = (log_root / workspace_id / "monitor.log.log").read_text()
    assert '"event": "monitor.dormancy_requested"' in lines
    assert '"event": "monitor.dormant"' in lines


@pytest.mark.parametrize("failure", ["connection", "commit_receipt"])
async def test_database_reconnect_publishes_pending_dormancy_failure_once(
    factory, wait, monkeypatch, failure
):
    from sqlalchemy.ext.asyncio import AsyncSession

    monitor, workspace_id, _, teardown = wait
    teardown.side_effect = OSError("synthetic compose dispatch error")
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = monitor.current_time - timedelta(minutes=1)

    get_for_update = WorkspaceRepository.get_for_update
    connection_lost = False

    async def temporarily_unavailable(repo, *args, **kwargs):
        nonlocal connection_lost
        if teardown.await_count and not connection_lost:
            connection_lost = True
            raise ConnectionRefusedError("synthetic outage")
        return await get_for_update(repo, *args, **kwargs)

    if failure == "connection":
        monkeypatch.setattr(WorkspaceRepository, "get_for_update", temporarily_unavailable)
    else:
        from awf.db.models import WorkspaceEvent

        commit = AsyncSession.commit
        lost = False

        async def lose_failure_receipt(session):
            nonlocal lost
            publishing = any(
                isinstance(row, WorkspaceEvent) and row.event_type == "workspace.dormancy_failed"
                for row in session.identity_map.values()
            )
            await commit(session)
            if publishing and not lost:
                lost = True
                raise ConnectionResetError("synthetic lost response")

        monkeypatch.setattr(AsyncSession, "commit", lose_failure_receipt)
    sleep = AsyncMock()
    await monitor.run(monitor.build(30, sleep=sleep))
    sleep.assert_awaited_once()
    teardown.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace.dormant_at is None
        (event,) = [
            event for event in workspace.events if event.event_type == "workspace.dormancy_failed"
        ]
        assert event.payload["failure_code"] == "OSError"


async def test_stale_inline_runner_cannot_clear_or_fail_a_dormant_wait(factory, wait):
    monitor, workspace_id, _, teardown = wait
    now = monitor.current_time
    start = now - timedelta(hours=1)
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.dormant_at = now
        workspace.awaiting_human_since = start
        workspace.awaiting_human_reason = "Review required"
        workspace.monitor_claimed_by = workspace.monitor_claim_expires_at = None
    stale = monitor.build(30)
    await stale._clear_workspace_attention(workspace_id)
    assert not await stale._terminate_failed(workspace_id, message="Stale callback")
    teardown.assert_not_awaited()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == "monitoring_pr"
        assert row.dormant_at == now
        assert row.awaiting_human_since == start
        assert row.awaiting_human_reason == "Review required"
