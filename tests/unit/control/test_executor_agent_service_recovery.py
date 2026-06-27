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
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.profiles.models import WorkspaceProfile


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


def _executor(*, side_effect: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        _run_agent_task_with_optional_planning=AsyncMock(side_effect=side_effect),
        _compose=SimpleNamespace(ensure_project_up=AsyncMock()),
        _mark_failed=AsyncMock(),
        _prepare_provider_recovery=AsyncMock(),
    )


async def _run_helper(
    executor: SimpleNamespace,
    tmp_path: Path,
) -> tuple[bool, object]:
    return await agent_service_recovery._run_agent_task_with_service_recovery(
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
    executor._mark_failed.assert_not_awaited()
    executor._prepare_provider_recovery.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_down_timeout_cleanup_failure_restarts_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executor = _executor(side_effect=[_cleanup_error(), "planning-ok"])
    command_evidence: list[str] = []

    async def _service_down(*_args: object, **_kwargs: object) -> bool:
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
        command_evidence=command_evidence,
        workspace_id="ws_agent_service",
    )

    assert recovered is True
    assert planning_failure == "planning-ok"
    executor._compose.ensure_project_up.assert_awaited_once()
    executor._mark_failed.assert_not_awaited()
    assert command_evidence == ['service "agent" is not running']


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
