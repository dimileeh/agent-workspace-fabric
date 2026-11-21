"""Operator controls for a suspended PR monitor."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from awf.control.executor import monitor_handoff, monitor_resume_runtime, runtime_setup_replay
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import ResourceReservation
from awf.db.repositories import MergeCandidateRepository, OperationRepository, WorkspaceRepository
from awf.node.compose_manager import ComposeManager
from awf.runtime.inspection import RuntimeSnapshot
from awf.runtime.operator_hints import operator_hint_from_threads
from awf.runtime.pr_monitor import AddressOperatorHint, WaitForCI
from awf.runtime.pr_monitor_runner import dormancy
from awf.runtime.pr_monitor_runner import runner as runner_module
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.service.controls import WorkspaceControlService
from awf.service.controls_errors import (
    WorkspaceGuideClaimNotActiveError,
    WorkspaceRemonitorMetadataMissingError,
)
from awf.service.controls_helpers import default_cleaner
from awf.service.gc import run_service_workspace_gc
from tests.integration.runtime.test_pr_monitor_dormancy import PollContinuesError
from tests.integration.runtime.test_pr_monitor_dormancy import wait as wait
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
)

pytest_plugins = ["tests.integration.runtime._pr_monitor_runner_fixtures"]

THRESHOLD = 30
DIRECTIVE = "Check the requested change"


async def suspend(factory, wait, *, sleep=None):
    monitor, workspace_id, _, _ = wait
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        row.human_wait_dormant_after_seconds = THRESHOLD
        row.awaiting_human_since = monitor.current_time - timedelta(seconds=THRESHOLD)
        row.node_id = "node"
    runner = monitor.build(THRESHOLD, sleep=sleep or AsyncMock(side_effect=PollContinuesError))
    await monitor.run(runner)


async def control(factory, workspace_id, action):
    async with factory.begin() as session:
        service = WorkspaceControlService(
            session, project_stopper=AsyncMock(), cleaner_factory=default_cleaner
        )
        return await getattr(service, f"{action}_workspace")(
            workspace_id,
            reason=None,
            idempotency_key=f"dormant-{action}",
            **({"directive": DIRECTIVE} if action == "guide" else {}),
        )


@pytest.mark.parametrize("action", ["remonitor", "guide"])
async def test_dormant_control_wakes_atomically(factory, wait, monkeypatch, action):
    monitor, workspace_id, _, _ = wait
    logs = Mock()
    monkeypatch.setattr(dormancy, "_log", logs)
    await suspend(factory, wait)
    assert [call.args[0] for call in logs.info.call_args_list] == [
        "monitor.dormancy_requested",
        "monitor.dormant",
    ]
    async with factory() as session:
        dormant_version = (await WorkspaceRepository(session).get(workspace_id)).version
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=AsyncMock(),
        config=WorkerConfig(node_id="node"),
    )
    assert not await worker._claim_monitoring_pr(workspace_id)

    first = await control(factory, workspace_id, action)
    replay = await control(factory, workspace_id, action)
    assert first.operation_id == replay.operation_id
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.dormant_at is None
        assert row.awaiting_human_since is None
        assert row.monitor_claimed_by is None
        assert row.version == dormant_version + 1
        assert not monitor.runtime_containers
        (event,) = [e for e in row.events if e.event_type == "workspace.dormant_woken"]
        assert event.reason_code == "HUMAN_WAIT_DORMANT_WOKEN"
        assert event.payload == {
            "control": action,
            "operation_id": first.operation_id,
        }
        if action == "guide":
            assert operator_hint_from_threads(row.monitor_threads_addressed).directive == DIRECTIVE
    assert await worker._claim_monitoring_pr(workspace_id)


async def test_guide_waiting_on_suspend_lock_wakes_committed_dormancy(
    factory, wait, cmd, adapter, monkeypatch, tmp_path
):
    monitor, workspace_id, _, teardown = wait
    entered_teardown, finish_teardown, read_payload = (asyncio.Event() for _ in range(3))
    original_teardown = teardown.side_effect

    async def pause_teardown(*args, **kwargs):
        entered_teardown.set()
        await finish_teardown.wait()
        return await original_teardown(*args, **kwargs)

    teardown.side_effect = pause_teardown
    sleeper = asyncio.create_task(suspend(factory, wait))
    guide_pid = None

    async def guide():
        nonlocal guide_pid
        async with factory.begin() as session:
            guide_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            read_payload.set()
            return await WorkspaceControlService(
                session, project_stopper=AsyncMock(), cleaner_factory=default_cleaner
            ).guide_workspace(workspace_id, directive=DIRECTIVE, reason=None)

    request = None
    try:
        async with asyncio.timeout(5):
            await entered_teardown.wait()
            request = asyncio.create_task(guide())
            await read_payload.wait()
            async with factory() as observer:
                while not await observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": guide_pid}
                ):
                    await asyncio.sleep(0)
    finally:
        finish_teardown.set()
        await sleeper
        if request is not None:
            await request

    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.dormant_at is None
        assert operator_hint_from_threads(row.monitor_threads_addressed).directive == DIRECTIVE
        types = [event.event_type for event in row.events]
        assert types.index("workspace.dormant") < types.index("workspace.dormant_woken")

    # Resume through the ordinary worker. Stub the agent/verdict boundary and
    # git effects, leaving prompt construction and durable hint retirement real.
    runner = monitor.build(THRESHOLD, sleep=AsyncMock(side_effect=PollContinuesError))
    monkeypatch.setattr(
        runner_module,
        "decide",
        lambda _status, state, *_args, **_kwargs: (
            AddressOperatorHint(state.pending_operator_hint)
            if state.pending_operator_hint
            else WaitForCI()
        ),
    )

    async def agent_verdict(**kwargs):
        assert "agent" in monitor.runtime_containers
        await adapter.run(
            **{key: kwargs[key] for key in ("compose_project", "compose_file", "prompt")}
        )
        return VerdictResult(verdict="fix_committed")

    runner._invoke_cli_for_verdict_result = agent_verdict
    runner._pre_existing_dirty_repair_worktree_result = AsyncMock(return_value=None)
    runner._repair_operation_start_head_result = AsyncMock(return_value=("abc123", None))
    runner._protected_scope_push_block = AsyncMock(return_value=None)
    runner._validated_git_push_result = AsyncMock(
        return_value=_GitPushResult(pushed=True, failed=False, returncode=0)
    )
    runner._rev_parse_head = AsyncMock(return_value="repaired")
    worker, setup = resume_worker(factory, cmd, tmp_path, monkeypatch, runner)
    assert await worker._claim_monitoring_pr(workspace_id)
    await worker._safely_resume_claimed_pr_monitor(
        workspace_id, recovery_operation_id=worker._monitor_recovery_operation_ids[workspace_id]
    )
    setup.assert_awaited_once()
    assert len(adapter.calls) == 1 and DIRECTIVE in adapter.calls[0]
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert operator_hint_from_threads(row.monitor_threads_addressed) is None
        (repair,) = [op for op in row.operations if op.type == OperationType.comment_repair.value]
        assert repair.result["outcome"] == "operator_hint_pushed"


def resume_worker(factory, cmd, tmp_path, monkeypatch, runner):
    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        runtime_setup_replay, "capture_agent_container_identity", AsyncMock(return_value=None)
    )
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", setup)
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
        pr_monitor_factory=lambda *_args, **_kwargs: runner,
    )
    return ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=executor,
        config=WorkerConfig(node_id="node"),
    ), setup


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_waiting_live_monitor_keeps_ordinary_control_semantics(factory, wait, action):
    monitor, workspace_id, _, _ = wait
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        row.awaiting_human_since = monitor.current_time
    await control(factory, workspace_id, action)
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.awaiting_human_since is None and row.dormant_at is None
        assert row.monitor_claimed_by == ("monitor" if action == "guide" else None)
        assert not any(event.event_type == "workspace.dormant_woken" for event in row.events)


async def test_remonitor_refuses_dormant_workspace_with_missing_metadata(factory, wait, tmp_path):
    _, workspace_id, _, _ = wait
    await suspend(factory, wait)
    (tmp_path / "compose.yml").unlink()
    with pytest.raises(WorkspaceRemonitorMetadataMissingError) as error:
        await control(factory, workspace_id, "remonitor")
    assert error.value.detail["missing"] == ["compose_file_path"]
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.dormant_at is not None
        assert row.awaiting_human_since is not None


async def test_claim_gated_guide_refuses_dormant_workspace(factory, wait):
    _, workspace_id, _, _ = wait
    await suspend(factory, wait)
    async with factory.begin() as session:
        service = WorkspaceControlService(
            session, project_stopper=AsyncMock(), cleaner_factory=default_cleaner
        )
        with pytest.raises(WorkspaceGuideClaimNotActiveError):
            await service.guide_workspace(
                workspace_id, directive=DIRECTIVE, require_claim_active=True
            )
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.dormant_at is not None
        assert operator_hint_from_threads(row.monitor_threads_addressed) is None


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        ("stop", {}),
        ("cancel", {"stop_stack": True}),
        ("cancel", {"stop_stack": False}),
    ],
)
async def test_terminal_control_finishes_dormant_workspace(
    factory, wait, adapter, tmp_path, action, kwargs
):
    monitor, workspace_id, _, _ = wait
    work_dir = tmp_path / "managed"
    worktree = work_dir / "git" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    (worktree / "retained.txt").write_text("Workspace changes")
    compose = work_dir / "compose" / workspace_id / "compose.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text((tmp_path / "compose.yml").read_text())
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        row.compose_file_path = str(compose)
    await suspend(factory, wait)
    async with factory.begin() as session:
        result = await getattr(
            WorkspaceControlService(
                session, project_stopper=AsyncMock(), cleaner_factory=default_cleaner
            ),
            f"{action}_workspace",
        )(workspace_id, reason=None, **kwargs)
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert row.status == row.task_attempt.status == "cancelled"
        assert row.dormant_at is None
        assert row.awaiting_human_since is None
        assert (await session.get(ResourceReservation, "human-wait-reservation")).released_at
        assert (
            next(op for op in row.operations if op.id == result.operation_id).status == "succeeded"
        )
        assert row.monitor_claimed_by is None
        candidate = await MergeCandidateRepository(
            session
        ).get_latest_for_workspace_with_merge_inputs(workspace_id)
        assert candidate.status == "closed"
        assert not any(event.event_type == "workspace.dormant_woken" for event in row.events)
    assert all(command[0] == "down" for command in monitor.runtime_commands)
    assert adapter.calls == []
    assert monitor.runtime_volumes == ({"data"} if kwargs.get("stop_stack") is False else set())
    assert worktree.exists()

    gc = await run_service_workspace_gc(
        factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=monitor.current_time + timedelta(days=2),
        include_statuses=["cancelled"],
        compose_manager=ComposeManager(work_dir=work_dir, template_path=tmp_path / "unused"),
    )
    assert gc.status == "succeeded"
    assert not worktree.exists()
    assert not monitor.runtime_volumes
    assert all(command[0] == "down" for command in monitor.runtime_commands)
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        executor=AsyncMock(),
        config=WorkerConfig(node_id="node"),
    )
    assert not await worker._claim_monitoring_pr(workspace_id)


async def test_failed_suspension_waits_one_full_threshold(
    factory, wait, cmd, tmp_path, monkeypatch
):
    from awf.service.secret_leases import SecretLeaseService

    monitor, workspace_id, forge, teardown = wait
    revoke = AsyncMock(side_effect=OSError("lease revocation unavailable"))
    monkeypatch.setattr(SecretLeaseService, "revoke_workspace_secret_leases", revoke)
    sink = AsyncMock(stream_id="monitor.log")
    original_build = monitor.build

    def with_logs(*args, **kwargs):
        runner = original_build(*args, **kwargs)
        runner._open_monitor_log = AsyncMock(return_value=sink)
        return runner

    monkeypatch.setattr(monitor, "build", with_logs)
    await suspend(factory, wait)
    teardown.assert_awaited_once()
    async with factory() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        (failure,) = [e for e in row.events if e.event_type == "workspace.dormancy_failed"]
        assert row.dormant_at is None
    monitor.current_time = failure.occurred_at + timedelta(seconds=THRESHOLD, microseconds=-2)

    async def tick(_seconds):
        assert "agent" in monitor.runtime_containers
        teardown.assert_awaited_once()
        assert (
            sum("monitor.dormancy_requested" in call.args[0] for call in sink.write.await_args_list)
            == 1
        )
        monitor.current_time += timedelta(microseconds=1)

    poll = AsyncMock(side_effect=tick)
    worker, setup = resume_worker(
        factory, cmd, tmp_path, monkeypatch, monitor.build(THRESHOLD, sleep=poll)
    )
    worker._worker_id = "monitor"
    await worker._release_monitoring_pr_claim(workspace_id)
    assert await worker._claim_monitoring_pr(workspace_id)
    await worker._safely_resume_claimed_pr_monitor(
        workspace_id, recovery_operation_id=worker._monitor_recovery_operation_ids[workspace_id]
    )
    assert poll.await_count == 2
    setup.assert_awaited_once()
    assert teardown.await_count == revoke.await_count == 2
    assert (
        sum("monitor.dormancy_requested" in call.args[0] for call in sink.write.await_args_list)
        == 2
    )
    forge.post_comment.assert_awaited_once()


async def test_open_operation_blocks_dormancy_once_across_polls(factory, wait):
    monitor, workspace_id, _, teardown = wait
    async with factory.begin() as session:
        blocking = await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload={"source": "operator_api"},
        )
    sink = AsyncMock(stream_id="monitor.log")

    def runner_with_logs():
        runner = monitor.build(THRESHOLD, sleep=AsyncMock(side_effect=[None, PollContinuesError]))
        runner._open_monitor_log = AsyncMock(return_value=sink)
        return runner

    with pytest.raises(PollContinuesError):
        await suspend(factory, wait)
    for _ in range(2):
        with pytest.raises(PollContinuesError):
            await monitor.run(runner_with_logs())
    teardown.assert_not_awaited()
    assert not any(
        "monitor.dormancy_requested" in call.args[0] for call in sink.write.await_args_list
    )
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        (blocked,) = [e for e in row.events if e.event_type == "workspace.dormancy_blocked"]
        assert blocked.reason_code == "HUMAN_WAIT_DORMANCY_BLOCKED"
        assert blocked.payload["blocking_operation_ids"] == [blocking.id]
        operations = OperationRepository(session)
        await operations.finish(await operations.get(blocking.id), status=OperationStatus.succeeded)

    # Start a fresh episode. A new operation opens after preflight has
    # released the workspace lock for the log, before teardown can start.
    await control(factory, workspace_id, "remonitor")
    monitor.current_time += timedelta(minutes=1)
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        row.monitor_claimed_by = "monitor"
        row.monitor_claim_expires_at = monitor.current_time + timedelta(hours=1)
        row.awaiting_human_since = monitor.current_time - timedelta(seconds=THRESHOLD)

    later = None

    async def open_during_request_log(line):
        nonlocal later
        if "monitor.dormancy_requested" in line and later is None:
            async with factory.begin() as session:
                await WorkspaceRepository(session).get_for_update(workspace_id)
                later = await OperationRepository(session).create(
                    workspace_id=workspace_id,
                    operation_type=OperationType.rebase,
                    status=OperationStatus.pending,
                    payload={"source": "operator_api"},
                )

    sink.write.side_effect = open_during_request_log
    with pytest.raises(PollContinuesError):
        await monitor.run(runner_with_logs())
    teardown.assert_not_awaited()
    assert later is not None
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(workspace_id)
        assert [
            e.payload["blocking_operation_ids"]
            for e in row.events
            if e.event_type == "workspace.dormancy_blocked"
        ] == [[blocking.id], [later.id]]
        operations = OperationRepository(session)
        await operations.finish(await operations.get(later.id), status=OperationStatus.succeeded)

    await monitor.run(runner_with_logs())
    teardown.assert_awaited_once()
    assert (
        sum("monitor.dormancy_requested" in call.args[0] for call in sink.write.await_args_list)
        == 2
    )
