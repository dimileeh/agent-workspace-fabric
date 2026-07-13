"""PR monitor agent compose-service recovery tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.git_manager import GitOperationError
from awf.profiles.models import ProfileDocker, WorkspaceProfile
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus, PRStatus
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
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
        services=(),
    )


@pytest.mark.unit
async def test_monitor_agent_idle_timeout_with_healthy_service_is_not_recovered(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    timeout_error = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="monitor idle timeout with service still healthy",
        ),
        reason_code=AGENT_IDLE_TIMEOUT,
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )
    adapter = FakeAdapter()
    adapter.queue(exc=timeout_error)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=True,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    command_evidence: list[str] = []

    with pytest.raises(AgentRunError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=command_evidence,
        )

    assert raised.value is timeout_error
    assert command_evidence == []
    ensure_project_up.assert_not_awaited()


@pytest.mark.unit
async def test_monitor_agent_hosted_timeout_skips_compose_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Hosted timeouts must not probe/restart the Compose agent service.

    Regression for PRRT_kwDOSJAM6s6PNKHp: in hosted mode an injected runtime
    executor owns process lifecycle, so there is no Compose agent service to
    restart. A timeout must re-raise unchanged instead of being
    misclassified as AGENT_SERVICE_UNHEALTHY and triggering Compose restarts
    that can terminate monitor recovery on GKE.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    timeout_error = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(
            returncode=124,
            stdout="",
            stderr="hosted wall-clock timeout",
        ),
        reason_code=AGENT_IDLE_TIMEOUT,
        details={"provider": "google", "model": "gemini-2.5-pro"},
    )
    # A non-None runtime_executor marks the adapter as hosted.
    adapter = FakeAdapter(runtime_executor=object())
    assert adapter.is_hosted is True
    adapter.queue(exc=timeout_error)
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

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentRunError) as raised,
    ):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=command_evidence,
        )

    assert raised.value is timeout_error
    assert command_evidence == []
    probe.assert_not_awaited()
    ensure_project_up.assert_not_awaited()
    assert adapter.hosted_pr_identities
    timeout_identity = adapter.hosted_pr_identities[0]
    assert timeout_identity is not None
    assert timeout_identity["repo_url"] == "git@github.com:dimileeh/aira-web.git"
    assert timeout_identity["pr_url"] == "https://github.com/dimileeh/aira-web/pull/42"
    assert {
        "event": "monitor.agent_service_recovery_skipped_hosted",
        "workspace_id": workspace_id,
        "reason_code": AGENT_IDLE_TIMEOUT,
        "hosted": True,
        "log_level": "warning",
    } in captured


@pytest.mark.unit
async def test_monitor_agent_hosted_terminal_head_gates_synced_delta_before_accepting(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """PRRT_kwDOSJAM6s6QSGqD: hosted pushed heads must pass local policy gates."""
    workspace_id = await seed_monitoring_workspace(factory)
    previous_head = "a" * 40
    terminal_head = "b" * 40
    changed_paths = ("pyproject.toml", "uv.lock")
    adapter = FakeAdapter(runtime_executor=object())
    adapter._queued.append(
        AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair",
            stderr="",
            terminal_head_sha=terminal_head,
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result()  # git fetch
    cmd.queue_result(stdout=f"{terminal_head}\n")  # git rev-parse FETCH_HEAD
    cmd.queue_result()  # git reset --hard
    cmd.queue_result(stdout="M\0pyproject.toml\0M\0uv.lock\0")  # hosted delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    supply_chain_calls: list[tuple[str, ...]] = []
    protected_calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> None:
        supply_chain_calls.append(tuple(kwargs["changed_paths"]))  # type: ignore[arg-type]

    async def _hosted_terminal_head_protected_scope_violations(
        _runner: object,
        **kwargs: object,
    ) -> list[object]:
        protected_calls.append(
            (
                str(kwargs["base_ref"]),
                str(kwargs["terminal_head_sha"]),
                tuple(kwargs["changed_paths"]),  # type: ignore[arg-type]
            )
        )
        return []

    runner._refresh_supply_chain_policy_before_push = (  # type: ignore[method-assign]
        _refresh_supply_chain_policy_before_push
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_hosted_terminal_head_protected_scope_violations",
        side_effect=_hosted_terminal_head_protected_scope_violations,
        create=True,
    )
    state = SimpleNamespace(last_push_sha=previous_head)

    result = await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=_write_compose_file(tmp_path),
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=["agent evidence"],
        operation_start_head=previous_head,
        state=state,
    )

    assert result.stdout == "AWF-VERDICT: FIXED: hosted repair"
    assert supply_chain_calls == [changed_paths]
    assert protected_calls == [(previous_head, terminal_head, changed_paths)]
    assert state.last_push_sha == terminal_head


@pytest.mark.unit
async def test_monitor_agent_hosted_terminal_head_policy_block_keeps_previous_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    previous_head = "a" * 40
    terminal_head = "b" * 40
    adapter = FakeAdapter(runtime_executor=object())
    adapter._queued.append(
        AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair",
            stderr="",
            terminal_head_sha=terminal_head,
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result()  # git fetch
    cmd.queue_result(stdout=f"{terminal_head}\n")  # git rev-parse FETCH_HEAD
    cmd.queue_result()  # git reset --hard
    cmd.queue_result(stdout="M\0.github/workflows/ci.yml\0")  # hosted delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _refresh_supply_chain_policy_before_push(**_kwargs: object) -> None:
        return None

    async def _hosted_terminal_head_protected_scope_violations(
        _runner: object,
        **_kwargs: object,
    ) -> list[object]:
        return [
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ]

    runner._refresh_supply_chain_policy_before_push = (  # type: ignore[method-assign]
        _refresh_supply_chain_policy_before_push
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_hosted_terminal_head_protected_scope_violations",
        side_effect=_hosted_terminal_head_protected_scope_violations,
        create=True,
    )
    state = SimpleNamespace(last_push_sha=previous_head)

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            operation_start_head=previous_head,
            state=state,
        )

    assert "agent changed protected quality-gate file" in str(raised.value)
    assert state.last_push_sha == previous_head


@pytest.mark.unit
async def test_monitor_agent_hosted_terminal_head_supply_chain_block_keeps_previous_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    previous_head = "a" * 40
    terminal_head = "b" * 40
    adapter = FakeAdapter(runtime_executor=object())
    adapter._queued.append(
        AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair",
            stderr="",
            terminal_head_sha=terminal_head,
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result()  # git fetch
    cmd.queue_result(stdout=f"{terminal_head}\n")  # git rev-parse FETCH_HEAD
    cmd.queue_result()  # git reset --hard
    cmd.queue_result(stdout="M\0uv.lock\0")  # hosted delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _refresh_supply_chain_policy_before_push(**_kwargs: object) -> str:
        return "Supply-chain policy blocked PR monitor publication: LOCKFILE_CHANGED"

    async def _hosted_terminal_head_protected_scope_violations(
        _runner: object,
        **_kwargs: object,
    ) -> list[object]:
        pytest.fail("protected-scope gate must not run after supply-chain block")

    runner._refresh_supply_chain_policy_before_push = (  # type: ignore[method-assign]
        _refresh_supply_chain_policy_before_push
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_hosted_terminal_head_protected_scope_violations",
        side_effect=_hosted_terminal_head_protected_scope_violations,
        create=True,
    )
    state = SimpleNamespace(last_push_sha=previous_head)

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            operation_start_head=previous_head,
            state=state,
        )

    assert "LOCKFILE_CHANGED" in str(raised.value)
    assert state.last_push_sha == previous_head


@pytest.mark.unit
async def test_monitor_agent_hosted_terminal_head_delta_unavailable_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    previous_head = "a" * 40
    terminal_head = "b" * 40
    adapter = FakeAdapter(runtime_executor=object())
    adapter._queued.append(
        AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair",
            stderr="",
            terminal_head_sha=terminal_head,
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result()  # git fetch
    cmd.queue_result(stdout=f"{terminal_head}\n")  # git rev-parse FETCH_HEAD
    cmd.queue_result()  # git reset --hard
    cmd.queue_result(returncode=1, stderr="fatal: bad revision")  # hosted delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = SimpleNamespace(last_push_sha=previous_head)

    with pytest.raises(AgentRunError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            operation_start_head=previous_head,
            state=state,
        )

    assert raised.value.reason_code == "HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE"
    assert state.last_push_sha == previous_head


@pytest.mark.unit
async def test_hosted_terminal_head_delta_paths_skip_diff_for_unchanged_head(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=cmd,
            adapter=SimpleNamespace(name=AgentRuntime.codex),
        )
    )
    sha = "a" * 40

    paths = await agent_service_recovery._hosted_terminal_head_delta_paths(
        runner,
        workspace_id="ws_hosted",
        worktree_path=tmp_path / "worktrees" / "ws_hosted",
        base_ref=sha.upper(),
        terminal_head_sha=sha,
    )

    assert paths == ()
    assert cmd.calls == []


@pytest.mark.unit
async def test_hosted_terminal_head_delta_paths_wrap_malformed_name_status_output(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout="M\tsrc/app.py\n")
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=cmd,
            adapter=SimpleNamespace(name=AgentRuntime.codex),
        )
    )

    with pytest.raises(AgentRunError) as raised:
        await agent_service_recovery._hosted_terminal_head_delta_paths(
            runner,
            workspace_id="ws_hosted",
            worktree_path=tmp_path / "worktrees" / "ws_hosted",
            base_ref="a" * 40,
            terminal_head_sha="b" * 40,
        )

    assert raised.value.reason_code == "HOSTED_REMOTE_HEAD_DELTA_UNAVAILABLE"
    assert "hosted repair terminal head delta was malformed" in raised.value.result.stderr
    assert isinstance(raised.value.__cause__, ProtectedScopeDiffError)


@pytest.mark.unit
async def test_gate_hosted_terminal_head_delta_blocks_when_protected_scope_diff_unavailable(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout="M\0src/app.py\0")
    supply_chain_calls: list[tuple[str, ...]] = []

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> None:
        supply_chain_calls.append(tuple(kwargs["changed_paths"]))  # type: ignore[arg-type]

    async def _protected_scope_unavailable(*_args: object, **_kwargs: object) -> list[object]:
        raise ProtectedScopeDiffError("diff baseline unavailable")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            runner=cmd,
            adapter=SimpleNamespace(name=AgentRuntime.codex),
        ),
        _refresh_supply_chain_policy_before_push=_refresh_supply_chain_policy_before_push,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_hosted_terminal_head_protected_scope_violations",
        side_effect=_protected_scope_unavailable,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await agent_service_recovery._gate_hosted_terminal_head_delta(
            runner,
            workspace_id="ws_hosted",
            worktree_path=tmp_path / "worktrees" / "ws_hosted",
            base_ref="a" * 40,
            terminal_head_sha="b" * 40,
            command_evidence=(),
        )

    assert raised.value.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "diff baseline unavailable" in str(raised.value)
    assert supply_chain_calls == [("src/app.py",)]


@pytest.mark.unit
async def test_hosted_terminal_head_protected_scope_violations_fail_when_workspace_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError, match="Workspace row ws_missing"):
        await agent_service_recovery._hosted_terminal_head_protected_scope_violations(
            runner,
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            base_ref="a" * 40,
            terminal_head_sha="b" * 40,
            changed_paths=("src/app.py",),
        )


@pytest.mark.unit
async def test_hosted_terminal_head_protected_scope_violations_wrap_file_diff_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "protected_file_diffs_for_committed_paths",
        side_effect=RuntimeError("git show failed"),
    )

    with pytest.raises(ProtectedScopeDiffError) as raised:
        await agent_service_recovery._hosted_terminal_head_protected_scope_violations(
            runner,
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktrees" / workspace_id,
            base_ref="a" * 40,
            terminal_head_sha="b" * 40,
            changed_paths=("src/app.py",),
        )

    assert "Could not read hosted terminal-head protected-scope file contents" in str(raised.value)
    assert "git show failed" in str(raised.value)


@pytest.mark.unit
async def test_monitor_agent_hosted_terminal_head_uses_current_head_when_start_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    current_head = "c" * 40
    terminal_head = "d" * 40
    adapter = FakeAdapter(runtime_executor=object())
    adapter._queued.append(
        AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: hosted repair",
            stderr="",
            terminal_head_sha=terminal_head,
        )
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(stdout=f"{current_head}\n")  # git rev-parse HEAD
    cmd.queue_result()  # git fetch
    cmd.queue_result(stdout=f"{terminal_head}\n")  # git rev-parse FETCH_HEAD
    cmd.queue_result()  # git reset --hard
    cmd.queue_result(stdout="M\0src/app.py\0")  # hosted delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    supply_chain_calls: list[tuple[str, ...]] = []
    protected_calls: list[str] = []

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> None:
        supply_chain_calls.append(tuple(kwargs["changed_paths"]))  # type: ignore[arg-type]

    async def _hosted_terminal_head_protected_scope_violations(
        _runner: object,
        **kwargs: object,
    ) -> list[object]:
        protected_calls.append(str(kwargs["base_ref"]))
        return []

    runner._refresh_supply_chain_policy_before_push = (  # type: ignore[method-assign]
        _refresh_supply_chain_policy_before_push
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_hosted_terminal_head_protected_scope_violations",
        side_effect=_hosted_terminal_head_protected_scope_violations,
        create=True,
    )

    await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=_write_compose_file(tmp_path),
        prompt="fix the comment",
        log_source="recovery",
        operation_start_head=None,
    )

    assert supply_chain_calls == [("src/app.py",)]
    assert protected_calls == [current_head]


@pytest.mark.unit
async def test_hosted_terminal_head_protected_scope_violations_use_committed_delta(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(runtime_executor=object()),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[object, ...]]] = []
    expected_violation = QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/**",
    )

    async def _protected_file_diffs_for_committed_paths(
        _cmd: object,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(
            (
                str(kwargs["base_ref"]),
                tuple(kwargs["changed_paths"]),  # type: ignore[arg-type]
                tuple(kwargs["owned_paths"]),  # type: ignore[arg-type]
                (),
            )
        )
        return {}

    def _find_protected_quality_gate_changes(**kwargs: object) -> list[QualityGateViolation]:
        base_ref, changed_paths, owned_paths, _grants = calls.pop()
        calls.append(
            (
                base_ref,
                changed_paths,
                owned_paths,
                tuple(kwargs["operator_granted_paths"]),  # type: ignore[arg-type]
            )
        )
        return [expected_violation]

    async def _active_operator_grant_specs(_workspace_id: str) -> tuple[str, ...]:
        return ("grant",)

    runner._active_operator_grant_specs = _active_operator_grant_specs  # type: ignore[method-assign]
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "protected_file_diffs_for_committed_paths",
        side_effect=_protected_file_diffs_for_committed_paths,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.find_protected_quality_gate_changes",
        side_effect=_find_protected_quality_gate_changes,
    )

    violations = await agent_service_recovery._hosted_terminal_head_protected_scope_violations(
        runner,
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        base_ref="a" * 40,
        terminal_head_sha="b" * 40,
        changed_paths=(".github/workflows/ci.yml",),
    )

    assert violations == [expected_violation]
    assert calls == [
        (
            "a" * 40,
            (".github/workflows/ci.yml",),
            (),
            ("grant",),
        )
    ]


@pytest.mark.unit
async def test_monitor_agent_hosted_cleanup_failure_skips_compose_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """Hosted cleanup-failure cases must not probe/restart the Compose agent service.

    Regression for PRRT_kwDOSJAM6s6POJS1: in hosted mode an injected runtime
    executor owns process lifecycle, so there is no Compose agent service to
    probe or restart. A ``ComposeExecCleanupError`` must re-raise unchanged
    instead of being remapped to ``AGENT_SERVICE_UNHEALTHY`` and triggering
    Compose restarts that can fail/terminate monitor recovery on GKE.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cleanup_error = ComposeExecCleanupError(
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
    # A non-None runtime_executor marks the adapter as hosted.
    adapter = FakeAdapter(runtime_executor=object())
    assert adapter.is_hosted is True
    adapter.queue(exc=cleanup_error)
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

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ComposeExecCleanupError) as raised,
    ):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=command_evidence,
        )

    assert raised.value is cleanup_error
    assert command_evidence == []
    probe.assert_not_awaited()
    ensure_project_up.assert_not_awaited()
    assert adapter.hosted_pr_identities
    cleanup_identity = adapter.hosted_pr_identities[0]
    assert cleanup_identity is not None
    assert cleanup_identity["repo_url"] == "git@github.com:dimileeh/aira-web.git"
    assert cleanup_identity["pr_url"] == "https://github.com/dimileeh/aira-web/pull/42"
    assert {
        "event": "monitor.agent_service_recovery_skipped_hosted",
        "workspace_id": workspace_id,
        "reason_code": cleanup_error.reason_code,
        "hosted": True,
        "log_level": "warning",
    } in captured


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
async def test_monitor_agent_service_recovery_fences_pre_retry_repairs_after_restart(
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

    async def _provider_recovery_suppresses_cli(_workspace_id: str) -> bool:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.monitor_claimed_by = "worker-new"
            await session.commit()
        return False

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        pytest.fail("superseded monitor must not repair agent-runtime ownership")

    runner._provider_recovery_suppresses_cli = _provider_recovery_suppresses_cli  # type: ignore[method-assign]
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        return_value=None,
    )
    ownership_repair = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.repair_agent_runtime_ownership",
        side_effect=_repair_agent_runtime_ownership,
    )

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError) as raised:
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=_write_compose_file(tmp_path),
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    assert adapter.calls == ["fix the comment"]
    ensure_project_up.assert_awaited_once()
    ownership_repair.assert_not_awaited()
    assert raised.value.details["superseded_reason"] == "monitor_claim_changed"
    assert raised.value.details["restart_attempts"] == 1


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
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._raise_if_monitor_agent_service_recovery_was_superseded",
        return_value=None,
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
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._raise_if_monitor_agent_service_recovery_was_superseded",
        return_value=None,
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
        services=(),
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
