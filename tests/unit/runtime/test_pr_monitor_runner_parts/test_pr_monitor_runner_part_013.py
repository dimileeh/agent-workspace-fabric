"""PR monitor agent compose-service recovery tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitOperationError
from awf.profiles.models import ProfileDocker, WorkspaceProfile
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus, PRStatus
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
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


def _green_status(*, pr_number: int = 42, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _write_compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return compose_file


@pytest.mark.unit
async def test_run_returns_after_terminal_agent_service_recovery_sentinel(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _fetch_status_for_decision(**_kwargs: object) -> PRStatus:
        return _green_status()

    async def _refresh_pr_feedback_resolution_state(**_kwargs: object) -> bool:
        return False

    async def _resolve_addressed_outdated_threads(**_kwargs: object) -> None:
        return None

    async def _execute(**kwargs: object) -> bool:
        await runner._terminate_failed(
            str(kwargs["workspace_id"]),
            message="monitor: agent service unhealthy after restart attempts",
            reason_code=AGENT_SERVICE_UNHEALTHY,
        )
        raise _MonitorAgentServiceRecoveryFailedError("agent service unhealthy")

    runner._fetch_status_for_decision = _fetch_status_for_decision  # type: ignore[method-assign]
    runner._refresh_pr_feedback_resolution_state = (  # type: ignore[method-assign]
        _refresh_pr_feedback_resolution_state
    )
    runner._resolve_addressed_outdated_threads = (  # type: ignore[method-assign]
        _resolve_addressed_outdated_threads
    )
    runner._execute = _execute  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        unexpected_recovery_failures = [
            event for event in workspace.events if event.reason_code == "MONITOR_RECOVERY_FAILED"
        ]
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert workspace.status == WorkspaceStatus.failed.value
    assert len(unhealthy_events) == 1
    assert unexpected_recovery_failures == []


@pytest.mark.unit
async def test_monitor_agent_idle_timeout_restarts_service_and_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={
                "provider_recovery": {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                }
            },
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    probe = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    command_evidence: list[str] = []
    compose_file = _write_compose_file(tmp_path)

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=command_evidence,
    )

    assert result.stdout == "AWF-VERDICT: FIXED: restarted"
    assert adapter.calls == ["fix the comment", "fix the comment"]
    assert command_evidence == [
        "monitor idle timeout while agent service was down",
        "AWF-VERDICT: FIXED: restarted",
    ]
    probe.assert_awaited_once()
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=300,
        force_recreate=True,
        services=("agent",),
    )


@pytest.mark.unit
async def test_monitor_agent_service_recovery_reruns_pre_launch_guards_before_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    calls: list[str] = []

    class RecordingAdapter(FakeAdapter):
        async def run(self, **kwargs: object):  # type: ignore[no-untyped-def,override]
            calls.append("adapter.run")
            return await super().run(**kwargs)  # type: ignore[arg-type]

    adapter = RecordingAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mirror_path = tmp_path / "mirror.git"

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        calls.append("provider_suppression")
        return False

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        calls.append("ownership_repair")
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        calls.append("mirror_hooks_repair")
        return True

    async def _ensure_project_up(**_kwargs: object) -> None:
        calls.append("ensure_project_up")

    runner._provider_recovery_suppresses_cli = _provider_recovery_suppresses_cli  # type: ignore[method-assign]
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_ensure_project_up,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.mirror_path_for_worktree",
        return_value=mirror_path,
        create=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_agent_runtime_ownership",
        side_effect=_repair_agent_runtime_ownership,
        create=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_mirror_hooks_path",
        side_effect=_repair_mirror_hooks_path,
        create=True,
    )

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=_write_compose_file(tmp_path),
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )

    assert result.stdout == "AWF-VERDICT: FIXED: restarted"
    assert calls == [
        "adapter.run",
        "ensure_project_up",
        "provider_suppression",
        "ownership_repair",
        "mirror_hooks_repair",
        "adapter.run",
    ]


@pytest.mark.unit
async def test_monitor_agent_service_recovery_pre_retry_guard_respects_provider_suppression(
    tmp_path: Path,
) -> None:
    class Runner:
        _worktrees_root = tmp_path / "worktrees"

        async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
            return True

    with pytest.raises(ProviderRecoveryRetryError):
        await agent_service_recovery._rerun_monitor_agent_pre_launch_guards(
            Runner(),
            workspace_id="ws-provider-suppressed",
        )


@pytest.mark.unit
async def test_monitor_agent_service_recovery_pre_retry_guard_fails_closed_on_ownership_repair(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    class Runner:
        _worktrees_root = tmp_path / "worktrees"

        async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
            return False

    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_agent_runtime_ownership",
        return_value=False,
    )

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError):
        await agent_service_recovery._rerun_monitor_agent_pre_launch_guards(
            Runner(),
            workspace_id="ws-ownership-failed",
        )


@pytest.mark.unit
async def test_monitor_agent_service_recovery_pre_retry_guard_fails_closed_on_mirror_repair(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    class Runner:
        _worktrees_root = tmp_path / "worktrees"

        async def _provider_recovery_suppresses_cli(self, _workspace_id: str) -> bool:
            return False

    mirror_path = tmp_path / "mirror.git"
    repair_error = GitOperationError(
        operation="mirror.hooks_path_repair",
        returncode=1,
        stdout="",
        stderr="fatal: config unset failed",
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_agent_runtime_ownership",
        return_value=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.mirror_path_for_worktree",
        return_value=mirror_path,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_mirror_hooks_path",
        side_effect=repair_error,
    )

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await agent_service_recovery._rerun_monitor_agent_pre_launch_guards(
            Runner(),
            workspace_id="ws-mirror-failed",
        )


@pytest.mark.unit
async def test_monitor_agent_idle_timeout_uses_workspace_compose_timeout_for_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.resolved_profile = WorkspaceProfile(
            name="monitor-restart-timeout",
            docker=ProfileDocker(startup_timeout_seconds=420),
        ).model_dump(mode="json")
        workspace.task_policy = {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:backend.git",
                    "base_branch": "main",
                    "compose_up_timeout_seconds": 900,
                }
            ]
        }
        await session.commit()

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={
                "provider_recovery": {
                    "provider": "google",
                    "model": "gemini-2.5-pro",
                }
            },
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    compose_file = _write_compose_file(tmp_path)

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )

    assert result.stdout == "AWF-VERDICT: FIXED: restarted"
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=900,
        force_recreate=True,
        services=("agent",),
    )


@pytest.mark.unit
async def test_monitor_agent_restart_timeout_invalid_profile_uses_default(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.resolved_profile = {"name": ""}
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    timeout = await agent_service_recovery._monitor_agent_service_restart_timeout_seconds(
        runner,
        workspace_id=workspace_id,
    )

    assert timeout == 300


@pytest.mark.unit
async def test_monitor_agent_cleanup_service_down_restarts_service_and_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf-test-cleanup",
            source="agent",
            label="monitor",
            message='service "agent" is not running',
            cleanup_result=CommandResult(
                returncode=1,
                stdout="",
                stderr='service "agent" is not running',
            ),
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: cleanup restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    probe = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.verify_head_object_exists",
        return_value=True,
    )
    command_evidence: list[str] = []
    compose_file = tmp_path / "compose.yml"

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=compose_file,
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=command_evidence,
    )

    assert result.stdout == "AWF-VERDICT: FIXED: cleanup restarted"
    assert adapter.calls == ["fix the comment", "fix the comment"]
    assert command_evidence == [
        'service "agent" is not running',
        "AWF-VERDICT: FIXED: cleanup restarted",
    ]
    probe.assert_awaited_once()
    ensure_project_up.assert_awaited_once_with(
        project_name="proj",
        compose_file=compose_file,
        workspace_id=workspace_id,
        wait=True,
        compose_up_timeout_seconds=300,
        force_recreate=True,
        services=("agent",),
    )


@pytest.mark.unit
async def test_monitor_agent_cleanup_service_down_repairs_git_before_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    calls: list[str] = []

    class RecordingAdapter(FakeAdapter):
        async def run(self, **kwargs: object):  # type: ignore[no-untyped-def,override]
            calls.append("adapter.run")
            return await super().run(**kwargs)  # type: ignore[arg-type]

    adapter = RecordingAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf-test-cleanup",
            source="agent",
            label="monitor",
            message='service "agent" is not running',
            cleanup_result=CommandResult(
                returncode=1,
                stdout="",
                stderr='service "agent" is not running',
            ),
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: cleanup repaired")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mirror_path = tmp_path / "mirror.git"

    async def _ensure_project_up(**_kwargs: object) -> None:
        calls.append("ensure_project_up")

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        calls.append("mirror_hooks_repair")
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        calls.append("verify_head")
        return False

    async def _open_merge_candidate_head_sha(_workspace_id: str) -> str:
        calls.append("open_candidate_head")
        return "abc123"

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        calls.append("recover_missing_head")
        return "def456"

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        calls.append("ownership_repair")
        return True

    runner._open_merge_candidate_head_sha = _open_merge_candidate_head_sha  # type: ignore[method-assign]
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_ensure_project_up,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.mirror_path_for_worktree",
        return_value=mirror_path,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_mirror_hooks_path",
        side_effect=_repair_mirror_hooks_path,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.verify_head_object_exists",
        side_effect=_verify_head_object_exists,
        create=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._recover_missing_head_object_from_filesystem",
        side_effect=_recover_missing_head_object_from_filesystem,
        create=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_agent_runtime_ownership",
        side_effect=_repair_agent_runtime_ownership,
    )

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )

    assert result.stdout == "AWF-VERDICT: FIXED: cleanup repaired"
    assert calls == [
        "adapter.run",
        "mirror_hooks_repair",
        "verify_head",
        "open_candidate_head",
        "recover_missing_head",
        "ensure_project_up",
        "ownership_repair",
        "mirror_hooks_repair",
        "adapter.run",
    ]


@pytest.mark.unit
async def test_monitor_agent_cleanup_service_down_stops_when_missing_head_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf-test-cleanup",
            source="agent",
            label="monitor",
            message='service "agent" is not running',
            cleanup_result=CommandResult(
                returncode=1,
                stdout="",
                stderr='service "agent" is not running',
            ),
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _open_merge_candidate_head_sha(_workspace_id: str) -> str:
        return "abc123"

    runner._open_merge_candidate_head_sha = _open_merge_candidate_head_sha  # type: ignore[method-assign]
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.mirror_path_for_worktree",
        return_value=tmp_path / "mirror.git",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_mirror_hooks_path",
        return_value=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.verify_head_object_exists",
        return_value=False,
        create=True,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._recover_missing_head_object_from_filesystem",
        return_value=None,
        create=True,
    )

    with pytest.raises(_MonitorHeadObjectMissingError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_not_awaited()


@pytest.mark.unit
async def test_monitor_agent_cleanup_service_down_uses_exception_message_when_output_empty(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf-test-cleanup",
            source="agent",
            label="monitor",
            message='service "agent" is not running',
            cleanup_result=CommandResult(returncode=1, stdout="", stderr=""),
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: cleanup restarted")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.verify_head_object_exists",
        return_value=True,
    )

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )

    assert result.stdout == "AWF-VERDICT: FIXED: cleanup restarted"
    assert adapter.calls == ["fix the comment", "fix the comment"]
    ensure_project_up.assert_awaited_once()


@pytest.mark.unit
async def test_monitor_agent_service_recovery_stops_when_workspace_leaves_monitoring(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: should not run")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )

    async def _cancel_workspace_after_restart(*_args: object, **_kwargs: object) -> None:
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code="TEST_CANCELLED_DURING_RESTART",
            )
            await session.commit()

    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_cancel_workspace_after_restart,
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_awaited_once()


@pytest.mark.unit
async def test_monitor_agent_service_recovery_checks_monitor_claim_before_restart(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-new"
        await session.commit()

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: should not run")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "worker-old"
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_not_awaited()
    assert raised.value.details["superseded_reason"] == "monitor_claim_changed"
    assert raised.value.details["restart_attempts"] == 1


@pytest.mark.unit
async def test_monitor_agent_service_recovery_stops_when_monitor_claim_is_superseded(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-old"
        await session.commit()

    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    adapter.queue(stdout="AWF-VERDICT: FIXED: should not run")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._monitor_owner_id = "worker-old"
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )

    async def _supersede_monitor_claim_after_restart(*_args: object, **_kwargs: object) -> None:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.monitor_claimed_by = "worker-new"
            await session.commit()

    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_supersede_monitor_claim_after_restart,
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_awaited_once()


@pytest.mark.unit
async def test_monitor_agent_unrelated_cleanup_failure_is_not_recovered(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf-test-cleanup",
        source="agent",
        label="monitor",
        message="permission denied",
        cleanup_result=CommandResult(
            returncode=1,
            stdout="",
            stderr="permission denied",
        ),
    )
    adapter = FakeAdapter()
    adapter.queue(exc=cleanup_error)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )

    with pytest.raises(ComposeExecCleanupError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id="ws_monitor_cleanup_passthrough",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert raised.value is cleanup_error
    ensure_project_up.assert_not_awaited()


@pytest.mark.unit
async def test_monitor_agent_service_restart_failure_terminates_without_provider_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="compose unavailable",
        ),
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        event_types = [event.event_type for event in workspace.events]
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "infrastructure_failure"
    assert "workspace.provider_recovery_requested" not in event_types
    assert len(unhealthy_events) == 1
    assert unhealthy_events[0].event_type == "workspace.state_changed"
    assert unhealthy_events[0].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 1,
    }


@pytest.mark.unit
async def test_monitor_agent_service_restart_unexpected_error_propagates(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=RuntimeError("unexpected bug"),
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    ensure_project_up.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_monitor_agent_service_recovery_exhaustion_terminates_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    for _ in range(3):
        adapter.queue(
            exc=AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="monitor idle timeout while agent service stayed down",
                ),
                reason_code=AGENT_IDLE_TIMEOUT,
                details={"provider": "google", "model": "gemini-2.5-pro"},
            )
        )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert adapter.calls == ["fix the comment", "fix the comment", "fix the comment"]
    assert ensure_project_up.await_count == 2
    assert workspace.status == WorkspaceStatus.failed.value
    assert unhealthy_events[-1].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 2,
    }
