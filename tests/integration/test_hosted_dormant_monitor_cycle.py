"""Dormant PR monitors across accepted hosted-node boots."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_handoff, monitor_resume_runtime, runtime_setup_replay
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceSecretLease
from awf.db.repositories import (
    MergeCandidateRepository,
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.hosted_node.lifecycle_schemas import HostObservation
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.node.cleanup import WorkspaceCleaner
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitManager
from awf.node.stack_launcher import ComposeStackLauncher
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MonitorConfig,
    NotifyHuman,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
from awf.runtime.pr_monitor_runner import runner as runner_module
from tests.integration.runtime._pr_monitor_runner_fixtures import FakeAdapter
from tests.integration.test_hosted_lifecycle_parity import source_workspace
from tests.integration.test_hosted_lifecycle_reauthorization import install_pair, wake
from tests.integration.test_retained_work_acceptance import PREFIX, WS
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("result", ["merged", "waiting"])
async def test_hosted_monitor_wakes_after_two_empty_boots(
    authority, create_application, create_client, tmp_path, monkeypatch, result
):
    await source_workspace(authority, create_client, tmp_path)
    runtime, _, factory = authority
    client, headers = create_client
    now = datetime.now(UTC)
    threshold = timedelta(seconds=30)
    first_owner = "initial-monitor"
    async with factory.begin() as session:
        row = await WorkspaceRepository(session).get(WS)
        original = row.task_policy["hosted_node"]
        row.base_commit = "a" * 40
        row.human_wait_dormant_after_seconds = threshold.total_seconds()
        row.monitor_claimed_by = first_owner
        row.monitor_claim_expires_at = now + timedelta(hours=1)
        attempt = row.task_attempt
        attempt.status = "monitoring_pr"
        await WorkspaceRepository(session)._sync_merge_candidate_lifecycle(
            row, attempt=attempt, to=WorkspaceStatus.monitoring_pr
        )
        session.add(
            WorkspaceSecretLease(
                id="monitor-lease",
                attempt_id=attempt.id,
                workspace_id=WS,
                secret_name="example",
                kind="mount",
                target="/run/secrets/example",
                mode="ro",
                provider="protected",
                status="mounted",
                issued_at=now,
            )
        )
        compose_file = tmp_path / "compose" / WS / "compose.yml"
        compose_file.parent.mkdir(parents=True)
        row.compose_file_path = str(compose_file)
        compose_file.write_text("services:\n  agent:\n    image: example\n")
        compose_project = row.compose_project_name

    async def readiness():
        response = await client.get(PREFIX + "/readiness", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    stack_running = True
    runtime_commands = []

    async def compose(_manager, _project, _file, args, **kwargs):
        nonlocal stack_running
        runtime_commands.append(kwargs["operation"])
        stack_running = kwargs["operation"] == "up"

    async def inspect(_project):
        return RuntimeSnapshot(
            stack_state="running" if stack_running else "missing",
            services=[RuntimeService("agent", "agent", None, "running")] if stack_running else [],
        )

    async def observe_host(identity):
        return HostObservation(
            identity, datetime.now(UTC) + timedelta(seconds=30), not stack_running, True
        )

    async def empty_project(*args, **kwargs):
        assert not stack_running

    monkeypatch.setattr(ComposeManager, "_compose", compose)
    monkeypatch.setattr(ComposeManager, "remove_project_by_label", empty_project)
    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", inspect)
    monkeypatch.setattr(monitor_handoff, "lock_resumed_worktree_registration", AsyncMock())
    monkeypatch.setattr(
        runtime_setup_replay, "capture_agent_container_identity", AsyncMock(return_value=None)
    )

    async def replay_setup(*args, **kwargs):
        assert stack_running
        return True

    setup = AsyncMock(side_effect=replay_setup)
    monkeypatch.setattr(monitor_handoff, "replay_setup_on_monitor_resume", setup)

    cmd, adapter = FakeCommandRunner(), FakeAdapter()
    forge = DefaultMergeMethodGitHubClient(cmd)
    forge.post_comment = AsyncMock()
    waiting_status = PRStatus(42, "abc123", MergeableState.MERGEABLE, CheckState.SUCCESS, (), (), 0)
    merged_status = replace(waiting_status, merged=True, merge_commit_sha="b" * 40)
    status = waiting_status

    async def poll(*args, **kwargs):
        return status

    monkeypatch.setattr(runner_module, "observe_pr_head_sync", poll)
    monkeypatch.setattr(
        runner_module,
        "decide",
        lambda *args, **kwargs: (
            decide(*args, **kwargs) if args[0].merged else NotifyHuman(message="Approval required")
        ),
    )

    def runner(sleep):
        monitor = PullRequestMonitorRunner(
            session_factory=factory,
            runner=cmd,
            gh=forge,
            adapter=adapter,
            worktrees_root=tmp_path / "git" / "worktrees",
            now=lambda: now,
            sleep=sleep,
            monitor_config=MonitorConfig(
                human_wait_dormant_after_seconds=threshold.total_seconds()
            ),
        )
        monitor._fetch_status_for_decision = poll
        return monitor

    async def initial_wait(_seconds):
        nonlocal now
        assert stack_running
        now += threshold

    runtime.receiver.lifecycle.observe_host = observe_host
    assert not (await readiness())["quiescent"]
    await runner(initial_wait).run(
        workspace_id=WS,
        compose_project=compose_project,
        compose_file=compose_file,
        monitor_owner_id=first_owner,
    )
    assert (await readiness())["quiescent"]
    assert not stack_running

    try:
        initial_epoch = runtime.receiver.identity.boot_epoch
        for boot in (1, 2):
            runtime = await wake(authority, create_application, create_client)
            runtime.receiver.lifecycle.observe_host = observe_host
            boot_status = await readiness()
            assert boot_status["identity"]["boot_epoch"] == initial_epoch + boot
            assert boot_status["quiescent"] and boot_status["database_quiescent"]
            acceptance = boot_status["recovery_acceptance"]
            assert [
                acceptance[k]
                for k in ("queued_workspaces", "monitor_workspaces", "attention_workspaces")
            ] == [0, 0, 0]
            async with factory() as session:
                row = await WorkspaceRepository(session).get(WS)
                assert row.dormant_at == now and row.task_policy["hosted_node"] == original

        fresh = await install_pair(client, headers, original, runtime.receiver.identity)
        response = await client.post(
            f"/v1/workspaces/{WS}/remonitor",
            json={
                "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
                "hosted_reauthorization": {
                    "expected_association": original,
                    "replacement_association": fresh,
                },
            },
            headers={**headers, "Idempotency-Key": "wake-monitor"},
        )
        assert response.status_code == 200, response.text
        assert not stack_running

        now += timedelta(minutes=5)
        wait_started = now
        polls = 0

        async def resumed_wait(_seconds):
            nonlocal now, status, polls
            assert stack_running
            assert not (await readiness())["quiescent"]
            assert (await consumers.environment())["GH_TOKEN"] == "fresh-github"
            async with factory() as session:
                row = await WorkspaceRepository(session).get(WS)
                assert row.awaiting_human_since == wait_started
                assert row.monitor_claimed_by == worker._worker_id
                assert row.task_policy["hosted_node"] == fresh
            polls += 1
            if result == "merged":
                assert polls == 1
                status = merged_status
            elif polls == 1:
                now += threshold - timedelta(microseconds=1)
            else:
                assert polls == 2
                now += timedelta(microseconds=1)

        compose = ComposeManager(work_dir=tmp_path, template_path=tmp_path / "unused")
        executor = _make_executor(
            cmd,
            factory,
            tmp_path,
            compose=compose,
            pr_monitor_factory=lambda *_args, **_kwargs: runner(resumed_wait),
        )
        consumers = WorkspaceConsumers(runtime, tmp_path)
        executor._workspace_scope = consumers.scope
        executor._worktree_activity_git_roots = GitManager(
            tmp_path / "git"
        ).worktree_activity_git_roots
        executor._stack_launcher = ComposeStackLauncher(
            compose=compose,
            agent_runtime_image="example",
            protected_profile_mounts=consumers.profile_mounts,
            resource_limits=consumers.resource_limits,
        )
        worker = ControlWorker(
            session_factory=factory,
            provisioner=AsyncMock(),
            executor=executor,
            runtime_cleaner=WorkspaceCleaner(
                git=GitManager(tmp_path / "git"), compose=compose, auth_work_dir=tmp_path
            ),
            config=WorkerConfig(node_id=runtime.receiver.identity.owner),
        )
        assert await worker._claim_monitoring_pr(WS)
        recovery_id = worker._monitor_recovery_operation_ids[WS]
        await worker._safely_resume_claimed_pr_monitor(WS, recovery_operation_id=recovery_id)

        async with factory() as session:
            row = await WorkspaceRepository(session).get(WS)
            assert polls, (
                row.failure_message,
                runtime_commands,
                row.status,
                [(op.type, op.status, op.error_code) for op in row.operations],
                [e.reason_code for e in row.events],
            )
        expected_quiescent = await readiness()
        assert expected_quiescent["quiescent"], expected_quiescent["blockers"]
        assert not stack_running
        assert runtime_commands == ["down", "up", "down"]
        setup.assert_awaited_once()
        forge.post_comment.assert_awaited_once()
        assert adapter.calls == []
        async with factory() as session:
            row = await WorkspaceRepository(session).get(WS)
            candidate = await MergeCandidateRepository(
                session
            ).get_latest_for_workspace_with_merge_inputs(WS)
            assert row.monitor_claimed_by is None and row.execution_claimed_by is None
            assert all(op.status == "succeeded" for op in row.operations)
            reservation = await ResourceReservationRepository(session).active_for_workspace(WS)
            assert (reservation is None) == (result == "merged")
            assert (await session.get(WorkspaceSecretLease, "monitor-lease")).status == "revoked"
            if result == "merged":
                assert row.status == row.task_attempt.status == "completed"
                assert row.dormant_at is None and row.awaiting_human_since is None
                assert row.pr_merge_sha == merged_status.merge_commit_sha
                assert candidate.status == "merged"
            else:
                assert row.status == row.task_attempt.status == "monitoring_pr"
                assert row.dormant_at == wait_started + threshold
                assert row.awaiting_human_since == wait_started
                assert candidate.status == "open"
    finally:
        await runtime.receiver.close()
