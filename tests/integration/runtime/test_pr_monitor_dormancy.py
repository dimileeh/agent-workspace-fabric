"""Dormant human waits through the real monitor and transaction boundaries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.db.models import ResourceReservation, WorkspaceSecretLease
from awf.db.repositories import WorkerHeartbeatRepository, WorkspaceRepository
from awf.node.compose_manager import ComposeManager, ComposeTeardownResult
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MonitorConfig,
    NotifyHuman,
    PRStatus,
    WaitForCI,
)
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
from awf.runtime.pr_monitor_runner import runner as runner_module
from tests.integration.runtime._pr_monitor_runner_fixtures import _seed_monitoring_workspace
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient

pytest_plugins = ["tests.integration.runtime._pr_monitor_runner_fixtures"]


class PollContinuesError(Exception):
    """Stop the test when the runner schedules another ordinary poll."""


@pytest.fixture
async def wait(factory, cmd, adapter, tmp_path, monkeypatch, sleep_fn):
    now = datetime.now(UTC)
    workspace_id = await _seed_monitoring_workspace(
        factory, repo_url="https://github.com/example/repo.git"
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services:\n  agent:\n    image: example\n")
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.compose_file_path = str(compose_file)
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
            runner._fetch_status_for_decision = AsyncMock(return_value=status)
            return runner

        async def run(self, runner, **owner):
            await runner.run(
                workspace_id=workspace_id,
                compose_project=f"awf_{workspace_id}",
                compose_file=compose_file,
                **(owner or {"monitor_owner_id": "monitor"}),
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


@pytest.mark.parametrize("owner", ["inline", "hosted", "changed", "expired", "new_episode"])
async def test_existing_human_episode_and_ownership_on_resume(factory, wait, monkeypatch, owner):
    from awf.runtime.pr_monitor_runner import notify_human_loop

    monitor, workspace_id, forge, teardown = wait
    start = monitor.current_time - timedelta(minutes=2)
    run_kwargs = {"monitor_owner_id": "monitor"}
    async with factory.begin() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        workspace.human_wait_dormant_after_seconds = 30
        workspace.awaiting_human_since = start
        if owner == "inline":
            workspace.monitor_claimed_by = workspace.monitor_claim_expires_at = None
            workspace.execution_claimed_by = "executor"
            workspace.execution_claim_expires_at = monitor.current_time + timedelta(hours=1)
            workspace.execution_claim_epoch = 7
            run_kwargs = {"dormancy_execution_claim": ("executor", 7)}
        elif owner == "hosted":
            workspace.task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        elif owner == "changed":
            workspace.monitor_claimed_by = "successor"
        elif owner == "expired":
            workspace.monitor_claim_expires_at = start

    if owner == "new_episode":
        suspend = notify_human_loop.suspend_human_wait

        async def replace_episode(*args, **kwargs):
            async with factory.begin() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                workspace.awaiting_human_since = monitor.current_time
            return await suspend(*args, **kwargs)

        monkeypatch.setattr(notify_human_loop, "suspend_human_wait", replace_episode)

    runner = monitor.build(30, sleep=AsyncMock(side_effect=PollContinuesError))
    if owner == "new_episode":
        with pytest.raises(PollContinuesError):
            await monitor.run(runner, **run_kwargs)
    else:
        await monitor.run(runner, **run_kwargs)
    forge.post_comment.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if owner in ("inline", "hosted"):
            assert workspace.dormant_at == monitor.current_time
            assert workspace.monitor_claimed_by is None
            assert workspace.execution_claimed_by is None
            assert workspace.execution_claim_expires_at is None
            assert workspace.execution_claim_epoch == (7 if owner == "inline" else 0)
        else:
            assert workspace.dormant_at is None
            assert workspace.monitor_claimed_by == (
                "successor" if owner == "changed" else "monitor"
            )
            assert any(
                event.event_type == "workspace.dormancy_failed" for event in workspace.events
            )
    assert teardown.await_count == (1 if owner == "inline" else 0)


@pytest.mark.parametrize(
    "stage",
    [
        "compose_teardown",
        "operation_finish",
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
        assert event.payload["stage"] == stage
        assert event.payload["runtime_recovery_required"] == (stage != "monitor_log")
    assert any("monitor.dormancy_failed" in call.args[0] for call in sink.write.await_args_list)


async def test_dormant_monitor_is_excluded_from_scheduling_and_stale_recovery(
    factory, wait, cmd, tmp_path
):
    from awf.control.worker import ControlWorker, WorkerConfig
    from awf.control.worker.claims import (
        _monitor_recovery_terminal_finalize_status,
        _workspace_is_monitoring_pr,
    )
    from awf.db.enums import OperationStatus
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


async def test_failed_teardown_is_followed_by_worker_compose_recovery_before_work(
    factory, wait, cmd, monkeypatch, tmp_path
):
    from awf.control.executor import monitor_handoff, monitor_resume_runtime, runtime_setup_replay
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
    executor = _make_executor(
        cmd,
        factory,
        tmp_path,
        compose=ComposeManager(work_dir=tmp_path, template_path=tmp_path / "unused"),
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
    assert calls == ["sync_base"]
    assert any(args[0] == "up" for args in monitor.runtime_commands)
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
    calls = 0

    async def temporarily_unavailable(repo, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
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
