"""Executor agent-service recovery replays setup after recreating the agent (#787)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.control.executor import agent_service_recovery, runtime_setup_replay
from awf.control.executor.constants import (
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
)
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.session import make_session_factory
from awf.profiles.models import WorkspaceProfile
from tests.postgres import postgres_test_engine
from tests.unit.control.runtime_setup_replay_fakes import (
    TOOL_PATH,
    FakeAgentContainer,
    FakeCompose,
    FakeToolValidation,
    allow_ownership_repair,
    patch_identity,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture(autouse=True)
def _agent_service_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _service_down(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(agent_service_recovery, "probe_agent_service_health", _service_down)
    monkeypatch.setattr(
        runtime_setup_replay, "repair_agent_runtime_ownership", allow_ownership_repair
    )


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr='service "agent" is not running'),
        reason_code="AGENT_IDLE_TIMEOUT",
        details={"provider": "openai", "model": "gpt-5.3-codex"},
    )


def _executor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    container: FakeAgentContainer,
    validation: FakeToolValidation,
    recreate: bool = True,
    with_worktree: bool = True,
) -> SimpleNamespace:
    executor = SimpleNamespace(
        _compose=FakeCompose(container, recreate=recreate),
        _validation=validation,
        _session_factory=factory,
        _mark_failed=AsyncMock(),
        _recheck_status=AsyncMock(return_value=True),
    )
    if with_worktree:
        executor._provisioner = SimpleNamespace(
            get_worktree_path=lambda workspace_id: tmp_path / "worktrees" / workspace_id
        )
    return executor


def _run_agent(validation: FakeToolValidation, outcomes: list[Any]) -> Any:
    async def _run(_accept_existing_plan: bool) -> Any:
        validation.trace.append("agent")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _run


async def _recover(
    executor: SimpleNamespace,
    validation: FakeToolValidation,
    tmp_path: Path,
    workspace_id: str,
    outcomes: list[Any],
    **kwargs: Any,
) -> tuple[bool, Any]:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n")
    return await agent_service_recovery._run_agent_callable_with_service_recovery(
        executor,
        run_agent=_run_agent(validation, outcomes),
        workspace=SimpleNamespace(id=workspace_id, task_policy={}),
        profile=WorkspaceProfile(name="test"),
        compose_project=f"awf_{workspace_id}",
        compose_file=compose_file,
        model="gpt-5.3-codex",
        command_evidence=[],
        workspace_id=workspace_id,
        hold_writer_lock=False,
        **kwargs,
    )


async def _replay_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
            )
        )
        return list(rows.scalars())


def _provisioned_container() -> FakeAgentContainer:
    container = FakeAgentContainer()
    container.tmp.add(TOOL_PATH)
    return container


@pytest.mark.unit
async def test_agent_service_recreate_replays_setup_before_agent_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    executor = _executor(factory, tmp_path, container=container, validation=validation)

    recovered, result = await _recover(
        executor, validation, tmp_path, ws_id, [_timeout_error(), "agent-ok"]
    )

    assert (recovered, result) == (True, "agent-ok")
    assert executor._compose.calls[0]["services"] == ("agent",)
    assert executor._compose.calls[0]["force_recreate"] is True
    assert validation.trace == ["agent", "phases:setup,pre_agent", "agent"]
    assert TOOL_PATH in container.tmp
    events = await _replay_events(factory, ws_id)
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["trigger"] == "executor_agent_service_recovery"
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_agent_service_restart_without_recreate_does_not_replay(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    executor = _executor(
        factory, tmp_path, container=container, validation=validation, recreate=False
    )

    recovered, _result = await _recover(
        executor, validation, tmp_path, ws_id, [_timeout_error(), "agent-ok"]
    )

    assert recovered is True
    assert validation.trace == ["agent", "agent"]
    assert await _replay_events(factory, ws_id) == []


@pytest.mark.unit
async def test_failed_replay_marks_failed_and_does_not_retry_agent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    executor = _executor(factory, tmp_path, container=container, validation=validation)
    callback_calls: list[dict[str, Any]] = []

    async def _before_mark_failed(
        *, reason_code: str | None = None, details: Mapping[str, Any] | None = None
    ) -> None:
        callback_calls.append({"reason_code": reason_code, "details": details})

    recovered, result = await _recover(
        executor,
        validation,
        tmp_path,
        ws_id,
        [_timeout_error(), "never-run"],
        before_mark_failed=_before_mark_failed,
    )

    assert (recovered, result) == (False, None)
    assert validation.trace == ["agent", "phases:setup,pre_agent"]
    assert len(executor._compose.calls) == 1
    assert [call["reason_code"] for call in callback_calls] == [
        RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    ]
    executor._mark_failed.assert_awaited_once()
    kwargs = executor._mark_failed.await_args.kwargs
    assert kwargs["reason_code"] == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert kwargs["from_status"] == WorkspaceStatus.running
    assert kwargs["failure_reason"] == FailureReason.service_startup_failure
    assert kwargs["details"]["trigger"] == "executor_agent_service_recovery"
    assert kwargs["details"]["agent_service_recovery"]["restart_attempts"] == 1
    assert await _replay_events(factory, ws_id) == []


@pytest.mark.unit
async def test_failed_replay_lets_marking_callback_own_the_transition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    executor = _executor(factory, tmp_path, container=container, validation=validation)
    callback = AsyncMock()

    recovered, _result = await _recover(
        executor,
        validation,
        tmp_path,
        ws_id,
        [_timeout_error()],
        before_mark_failed=callback,
        before_mark_failed_marks_workspace=True,
        expected_status=WorkspaceStatus.validating,
        failure_from_status=WorkspaceStatus.validating,
    )

    assert recovered is False
    callback.assert_awaited_once()
    assert callback.await_args.kwargs["reason_code"] == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_failed_replay_respects_stale_status_fence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    executor = _executor(factory, tmp_path, container=container, validation=validation)

    async def _recheck(_workspace_id: str, *, action: str, **_kwargs: Any) -> bool:
        return action != "agent_service_setup_replay_failed"

    executor._recheck_status = _recheck

    recovered, _result = await _recover(executor, validation, tmp_path, ws_id, [_timeout_error()])

    assert recovered is False
    executor._mark_failed.assert_not_awaited()


@pytest.mark.unit
async def test_replay_without_worktree_path_fails_with_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_monitoring_pr(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    executor = _executor(
        factory, tmp_path, container=container, validation=validation, with_worktree=False
    )

    recovered, _result = await _recover(executor, validation, tmp_path, ws_id, [_timeout_error()])

    assert recovered is False
    assert validation.setup_runs() == 0
    kwargs = executor._mark_failed.await_args.kwargs
    assert kwargs["reason_code"] == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert kwargs["failure_reason"] == FailureReason.infrastructure_failure
