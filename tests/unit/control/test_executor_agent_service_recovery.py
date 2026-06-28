"""Executor recovery for dead agent compose services."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import agent_service_recovery
from awf.control.executor.types import _PlanningRunFailure
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.node.compose_manager import ComposeOperationError
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import AGENT_STALLED_IN_CONFORMANCE


def _timeout_error(reason_code: str) -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=124,
            stdout="",
            stderr='service "agent" is not running',
        ),
        reason_code=reason_code,
        details={
            "provider": "openai",
            "model": "gpt-5.3-codex",
            "provider_recovery": {
                "reason_code": reason_code,
                "failure_type": "idle_timeout",
                "failure_scope": "provider",
                "failure_fingerprint": "provider-fingerprint",
            },
        },
    )


def _cleanup_error() -> ComposeExecCleanupError:
    return ComposeExecCleanupError(
        invocation_id="agent-timeout-cleanup",
        source="agent",
        label="codex",
        message='service "agent" is not running',
        cleanup_result=CommandResult(
            returncode=1,
            stdout="",
            stderr='service "agent" is not running',
        ),
    )


def _cleanup_error_message_only() -> ComposeExecCleanupError:
    return ComposeExecCleanupError(
        invocation_id="agent-timeout-cleanup",
        source="agent",
        label="codex",
        message='service "agent" is not running',
        cleanup_result=CommandResult(returncode=1, stdout="", stderr=""),
    )


def _conformance_timeout_failure(reason_code: str) -> _PlanningRunFailure:
    return _PlanningRunFailure(
        message="plan conformance stalled in iteration 0 (no_output)",
        reason_code=AGENT_STALLED_IN_CONFORMANCE,
        details={
            "conformance_stall": {
                "reason_code": AGENT_STALLED_IN_CONFORMANCE,
                "source_reason_code": reason_code,
                "last_output_excerpt": 'service "agent" is not running',
            }
        },
    )


def _executor(*, side_effect: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        _run_agent_task_with_optional_planning=AsyncMock(side_effect=side_effect),
        _compose=SimpleNamespace(ensure_project_up=AsyncMock()),
        _mark_failed=AsyncMock(),
        _recheck_status=AsyncMock(return_value=True),
        _prepare_provider_recovery=AsyncMock(),
    )


async def _run_helper(
    executor: SimpleNamespace,
    tmp_path: Path,
    *,
    profile: WorkspaceProfile | None = None,
    workspace: SimpleNamespace | None = None,
    execution_owner_id: str | None = None,
) -> tuple[bool, object]:
    return await agent_service_recovery._run_agent_task_with_service_recovery(
        executor,
        adapter=SimpleNamespace(),
        workspace=workspace
        or SimpleNamespace(id="ws_agent_service", task_prompt="do it", task_tag=None),
        profile=profile or WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path,
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        execution_owner_id=execution_owner_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", ["AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT"])
async def test_agent_service_down_timeout_restarts_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason_code: str,
) -> None:
    executor = _executor(side_effect=[_timeout_error(reason_code), "planning-ok"])

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is True
    assert planning_failure == "planning-ok"
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.running,
        action="agent_service_restart_recovery",
        owner_id=None,
    )
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_agent_callable_service_recovery_uses_validation_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[])
    run_agent = AsyncMock(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "validation-ok"])

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=run_agent,
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is True
    assert result == "validation-ok"
    assert run_agent.await_count == 2
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.validating,
        action="agent_service_restart_recovery",
        owner_id=None,
    )
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_restart_uses_companion_aware_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "planning-ok"])
    profile = WorkspaceProfile(name="test")
    workspace = SimpleNamespace(
        id="ws_agent_service",
        task_prompt="do it",
        task_tag=None,
        task_policy={
            "companions": [
                {
                    "name": "slow-api",
                    "repo_url": "git@github.com:x/slow-api.git",
                    "compose_up_timeout_seconds": 900,
                }
            ],
        },
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(
        executor,
        tmp_path,
        profile=profile,
        workspace=workspace,
    )

    assert recovered is True
    assert planning_failure == "planning-ok"
    assert (
        executor._compose.ensure_project_up.await_args.kwargs["compose_up_timeout_seconds"] == 900
    )


@pytest.mark.unit
async def test_agent_service_restart_rechecks_running_status_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "planning-ok"])
    executor._recheck_status.return_value = False

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(
        executor,
        tmp_path,
        execution_owner_id="worker-1",
    )

    assert recovered is False
    assert planning_failure is None
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.running,
        action="agent_service_restart_recovery",
        owner_id="worker-1",
    )
    assert executor._run_agent_task_with_optional_planning.await_count == 1
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_recovery_callbacks_recheck_supplied_validation_status_before_retry(
    tmp_path: Path,
) -> None:
    executor = SimpleNamespace(
        _run_agent_git_writability_preflight=AsyncMock(return_value=True),
        _ensure_ollama_model_or_mark_failed=AsyncMock(return_value=True),
        _recheck_status=AsyncMock(return_value=True),
    )
    repair_calls: list[dict[str, object]] = []

    async def _repair_mirror_hooks_path_or_mark_failed(**kwargs: object) -> bool:
        repair_calls.append(kwargs)
        return True

    async def _repair_hooks_after_agent_cleanup_failure(**_kwargs: object) -> bool:
        return True

    async def _recover_missing_head_after_cleanup_failure(
        *_args: object,
        **_kwargs: object,
    ) -> bool:
        return True

    workspace = SimpleNamespace(owned_paths=[])
    before_agent_retry, _cleanup_repair = (
        agent_service_recovery._build_agent_service_recovery_callbacks(
            executor,
            workspace_id="ws_agent_service",
            workspace=workspace,
            compose_project="awf_ws_agent_service",
            compose_file=tmp_path / "compose.yml",
            worktree_path=tmp_path,
            execution_owner_id="worker-1",
            repair_mirror_hooks_path_or_mark_failed=_repair_mirror_hooks_path_or_mark_failed,
            repair_hooks_after_agent_cleanup_failure=_repair_hooks_after_agent_cleanup_failure,
            recover_missing_head_after_cleanup_failure=(
                _recover_missing_head_after_cleanup_failure
            ),
            deposit_planning_artifacts=lambda: None,
            expected_status=WorkspaceStatus.validating,
            cleanup_failure_from_status=WorkspaceStatus.validating,
        )
    )

    assert await before_agent_retry() is True
    executor._run_agent_git_writability_preflight.assert_awaited_once_with(
        workspace_id="ws_agent_service",
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path,
        from_status=WorkspaceStatus.validating,
    )
    executor._ensure_ollama_model_or_mark_failed.assert_awaited_once_with(
        workspace_id="ws_agent_service",
        ws=workspace,
        from_status=WorkspaceStatus.validating,
        return_reason_code=True,
    )
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.validating,
        action="agent_run",
        owner_id="worker-1",
    )
    assert len(repair_calls) == 1
    assert repair_calls[0]["failure_stage"] == "before agent retry"
    assert repair_calls[0]["before_mark_failed"] is not None
    assert repair_calls[0]["failure_from_status"] is WorkspaceStatus.validating
    assert repair_calls[0]["return_reason_code"] is True


@pytest.mark.unit
async def test_agent_service_retry_guard_failure_runs_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[])
    run_agent = AsyncMock(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "validation-ok"])
    callback_calls: list[str] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _before_agent_retry() -> bool:
        return False

    async def _before_mark_failed() -> None:
        callback_calls.append("finish-validation")

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=run_agent,
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        before_agent_retry=_before_agent_retry,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_calls == ["finish-validation"]
    assert run_agent.await_count == 1
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_retry_guard_failure_passes_reason_to_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[])
    run_agent = AsyncMock(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "validation-ok"])
    callback_reason_codes: list[str | None] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _before_agent_retry() -> str:
        return "GIT_AGENT_WRITABILITY_FAILED"

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=run_agent,
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        before_agent_retry=_before_agent_retry,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_reason_codes == ["GIT_AGENT_WRITABILITY_FAILED"]
    assert run_agent.await_count == 1
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_retry_guard_false_uses_recovery_abort_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[])
    run_agent = AsyncMock(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "validation-ok"])
    callback_reason_codes: list[str | None] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _before_agent_retry() -> bool:
        return False

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=run_agent,
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        before_agent_retry=_before_agent_retry,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_reason_codes == [agent_service_recovery.AGENT_SERVICE_RECOVERY_ABORTED]
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_restart_recheck_failure_runs_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[])
    executor._recheck_status.return_value = False
    callback_reason_codes: list[str | None] = []
    run_agent = AsyncMock(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT"), "validation-ok"])

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=run_agent,
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_reason_codes == ["EXECUTOR_STALE_STATUS"]
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.validating,
        action="agent_service_restart_recovery",
        owner_id=None,
    )
    assert run_agent.await_count == 1
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_conformance_timeout_failure_restarts_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(
        side_effect=[_conformance_timeout_failure("AGENT_IDLE_TIMEOUT"), "planning-ok"]
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is True
    assert planning_failure == "planning-ok"
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_conformance_timeout_failure_with_live_service_keeps_stall_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failure = _conformance_timeout_failure("AGENT_IDLE_TIMEOUT")
    executor = _executor(side_effect=[failure])

    async def _service_up(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_up)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is True
    assert planning_failure is failure
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_conformance_timeout_exhausts_to_infra_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(
        side_effect=[
            _conformance_timeout_failure("AGENT_TIMEOUT"),
            _conformance_timeout_failure("AGENT_TIMEOUT"),
            _conformance_timeout_failure("AGENT_TIMEOUT"),
        ]
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is False
    assert planning_failure is None
    assert executor._compose.ensure_project_up.await_count == 2
    executor._mark_failed.assert_awaited_once()
    recovery_details = executor._mark_failed.await_args.kwargs["details"]["agent_service_recovery"]
    assert recovery_details["source_reason_code"] == "AGENT_TIMEOUT"
    assert recovery_details["restart_attempts"] == 2


@pytest.mark.unit
async def test_agent_service_down_timeout_cleanup_failure_restarts_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_exc = _cleanup_error()
    executor = _executor(side_effect=[cleanup_exc, "planning-ok"])
    command_evidence: list[str] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    repair_calls: list[tuple[ComposeExecCleanupError, int]] = []

    async def _repair_after_cleanup_failure(exc: ComposeExecCleanupError) -> bool:
        repair_calls.append((exc, executor._compose.ensure_project_up.await_count))
        return True

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    (
        recovered,
        planning_failure,
    ) = await agent_service_recovery._run_agent_task_with_service_recovery(
        executor,
        adapter=SimpleNamespace(),
        workspace=SimpleNamespace(id="ws_agent_service", task_prompt="do it", task_tag=None),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path,
        model="gpt-5.3-codex",
        command_evidence=command_evidence,
        workspace_id="ws_agent_service",
        after_agent_cleanup_failure_repair=_repair_after_cleanup_failure,
    )

    assert recovered is True
    assert planning_failure == "planning-ok"
    assert repair_calls == [(cleanup_exc, 0)]
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()
    assert command_evidence == ['service "agent" is not running']


@pytest.mark.unit
async def test_agent_service_down_timeout_cleanup_message_only_restarts_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cleanup_exc = _cleanup_error_message_only()
    executor = _executor(side_effect=[cleanup_exc, "planning-ok"])

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is True
    assert planning_failure == "planning-ok"
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_timeout_cleanup_repair_failure_aborts_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_cleanup_error(), "planning-ok"])

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _repair_after_cleanup_failure(_exc: ComposeExecCleanupError) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    (
        recovered,
        planning_failure,
    ) = await agent_service_recovery._run_agent_task_with_service_recovery(
        executor,
        adapter=SimpleNamespace(),
        workspace=SimpleNamespace(id="ws_agent_service", task_prompt="do it", task_tag=None),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        worktree_path=tmp_path,
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        after_agent_cleanup_failure_repair=_repair_after_cleanup_failure,
    )

    assert recovered is False
    assert planning_failure is None
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_cleanup_repair_failure_runs_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_cleanup_error(), "planning-ok"])
    callback_calls: list[str] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _repair_after_cleanup_failure(_exc: ComposeExecCleanupError) -> bool:
        return False

    async def _before_mark_failed() -> None:
        callback_calls.append("finish-validation")

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=AsyncMock(side_effect=[_cleanup_error(), "planning-ok"]),
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        after_agent_cleanup_failure_repair=_repair_after_cleanup_failure,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_calls == ["finish-validation"]
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_cleanup_repair_failure_passes_reason_to_terminal_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_cleanup_error(), "planning-ok"])
    callback_reason_codes: list[str | None] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _repair_after_cleanup_failure(_exc: ComposeExecCleanupError) -> str:
        return "MIRROR_HOOKS_PATH_REPAIR_FAILED"

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=AsyncMock(side_effect=[_cleanup_error(), "planning-ok"]),
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        after_agent_cleanup_failure_repair=_repair_after_cleanup_failure,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_reason_codes == ["MIRROR_HOOKS_PATH_REPAIR_FAILED"]
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_cleanup_repair_false_uses_recovery_abort_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_cleanup_error(), "planning-ok"])
    callback_reason_codes: list[str | None] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _repair_after_cleanup_failure(_exc: ComposeExecCleanupError) -> bool:
        return False

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, result = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=AsyncMock(side_effect=[_cleanup_error(), "planning-ok"]),
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
        after_agent_cleanup_failure_repair=_repair_after_cleanup_failure,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    assert result is None
    assert callback_reason_codes == [agent_service_recovery.AGENT_SERVICE_RECOVERY_ABORTED]
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("service_healthy", [True, None])
async def test_agent_timeout_cleanup_failure_with_live_service_keeps_cleanup_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service_healthy: bool | None,
) -> None:
    exc = _cleanup_error()
    executor = _executor(side_effect=[exc])

    async def _probe(*_args: object, **_kwargs: object) -> bool | None:
        return service_healthy

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _probe)

    with pytest.raises(ComposeExecCleanupError) as raised:
        await _run_helper(executor, tmp_path)

    assert raised.value is exc
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("service_healthy", [True, None])
async def test_agent_timeout_with_healthy_or_indeterminate_service_keeps_provider_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service_healthy: bool | None,
) -> None:
    exc = _timeout_error("AGENT_IDLE_TIMEOUT")
    executor = _executor(side_effect=[exc])

    async def _probe(*_args: object, **_kwargs: object) -> bool | None:
        return service_healthy

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _probe)

    with pytest.raises(AgentRunError) as raised:
        await _run_helper(executor, tmp_path)

    assert raised.value is exc
    executor._compose.ensure_project_up.assert_not_awaited()
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_restart_budget_exhausts_to_infra_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(
        side_effect=[
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
        ]
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is False
    assert planning_failure is None
    assert executor._compose.ensure_project_up.await_count == 2
    executor._mark_failed.assert_awaited_once()
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["from_status"] is WorkspaceStatus.running
    assert mark_failed_kwargs["failure_reason"] is FailureReason.infrastructure_failure
    assert mark_failed_kwargs["reason_code"] == "AGENT_SERVICE_UNHEALTHY"
    assert mark_failed_kwargs["details"]["provider_recovery"]["reason_code"] == (
        "AGENT_SERVICE_UNHEALTHY"
    )
    assert mark_failed_kwargs["details"]["provider_recovery"]["failure_scope"] == "infra"
    assert mark_failed_kwargs["details"]["provider_recovery"]["failure_fingerprint"] == ""
    assert mark_failed_kwargs["details"]["agent_service_recovery"]["restart_attempts"] == 2
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_restart_budget_exhaustion_terminal_callback_gets_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(
        side_effect=[
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
        ]
    )
    callback_reason_codes: list[str | None] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _before_mark_failed(*, reason_code: str | None = None) -> None:
        callback_reason_codes.append(reason_code)

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    (
        recovered,
        planning_failure,
    ) = await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=AsyncMock(
            side_effect=[
                _timeout_error("AGENT_IDLE_TIMEOUT"),
                _timeout_error("AGENT_IDLE_TIMEOUT"),
                _timeout_error("AGENT_IDLE_TIMEOUT"),
            ]
        ),
        workspace=SimpleNamespace(id="ws_agent_service", task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project="awf_ws_agent_service",
        compose_file=tmp_path / "compose.yml",
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id="ws_agent_service",
        before_mark_failed=_before_mark_failed,
    )

    assert recovered is False
    assert planning_failure is None
    assert callback_reason_codes == ["AGENT_SERVICE_UNHEALTHY"]
    executor._mark_failed.assert_awaited_once()


@pytest.mark.unit
async def test_agent_service_down_restart_budget_exhaustion_respects_stale_owner_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(
        side_effect=[
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
            _timeout_error("AGENT_IDLE_TIMEOUT"),
        ]
    )
    executor._recheck_status.side_effect = [True, True, False]

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(
        executor,
        tmp_path,
        execution_owner_id="worker-stale",
    )

    assert recovered is False
    assert planning_failure is None
    executor._compose.ensure_project_up.assert_awaited()
    assert executor._recheck_status.await_count == 3
    executor._recheck_status.assert_awaited_with(
        "ws_agent_service",
        expected=WorkspaceStatus.running,
        action="agent_service_restart_terminal",
        owner_id="worker-stale",
    )
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_restart_failure_marks_infra_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT")])
    executor._compose.ensure_project_up.side_effect = ComposeOperationError(
        operation="up",
        returncode=1,
        stdout="",
        stderr="compose failed",
    )

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(executor, tmp_path)

    assert recovered is False
    assert planning_failure is None
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_awaited_once()
    mark_failed_kwargs = executor._mark_failed.await_args.kwargs
    assert mark_failed_kwargs["from_status"] is WorkspaceStatus.running
    assert mark_failed_kwargs["failure_reason"] is FailureReason.infrastructure_failure
    assert mark_failed_kwargs["message"].startswith("agent compose service restart failed:")
    assert "compose failed" in mark_failed_kwargs["message"]
    assert mark_failed_kwargs["reason_code"] == "AGENT_SERVICE_UNHEALTHY"
    assert mark_failed_kwargs["details"]["provider_recovery"]["reason_code"] == (
        "AGENT_SERVICE_UNHEALTHY"
    )
    recovery_details = mark_failed_kwargs["details"]["agent_service_recovery"]
    assert recovery_details["source_reason_code"] == "AGENT_IDLE_TIMEOUT"
    assert recovery_details["service_healthy"] is False
    assert recovery_details["restart_attempts"] == 1
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_restart_failure_respects_stale_owner_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT")])
    executor._compose.ensure_project_up.side_effect = ComposeOperationError(
        operation="up",
        returncode=1,
        stdout="",
        stderr="compose failed",
    )
    executor._recheck_status.return_value = False

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    recovered, planning_failure = await _run_helper(
        executor,
        tmp_path,
        execution_owner_id="worker-stale",
    )

    assert recovered is False
    assert planning_failure is None
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._recheck_status.assert_awaited_once_with(
        "ws_agent_service",
        expected=WorkspaceStatus.running,
        action="agent_service_restart_terminal",
        owner_id="worker-stale",
    )
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_restart_unexpected_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_timeout_error("AGENT_IDLE_TIMEOUT")])
    executor._compose.ensure_project_up.side_effect = RuntimeError("unexpected bug")

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await _run_helper(executor, tmp_path)

    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()
