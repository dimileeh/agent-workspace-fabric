"""Restart failures whose audit write failed still count toward the streak (#936).

A ``restart_failed`` event that could not be written is held in memory, written
ahead of the next restart attempt or ``recovered`` boundary, and still counts
toward the streak that parks the workspace at the limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_resume_restart_escalation
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_monitor_recovery_parts.test_monitor_resume_restart_escalation import (  # noqa: E501
    _POSTGRES_COMPOSE,
    _SECRET,
    _dependency_health,
    _events,
    _executor,
    _fail_failure_event_writes,
    _fail_the_next_streak_count,
    _guide,
    _noop_persist,
    _runtime_unusable,
    _ScriptedCompose,
    _seed,
    _status,
    _streak,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (_runtime_unusable, factory, fake)


@pytest.mark.unit
async def test_unrecorded_failure_is_persisted_before_the_next_restart(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    # The unrecorded failure still counts, so the streak parks at the limit.
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    for event in failed:
        assert event.reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
        assert event.payload is not None
        assert event.payload["reason_code"] == "COMPOSE_COMMAND_FAILED"
        assert _SECRET not in str(event.payload)
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_failures_that_cannot_be_recorded_still_park_at_the_limit(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=MONITOR_RUNTIME_RESTART_FAILURE_LIMIT)
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT
    # The park closes the streak, so its unrecorded failures are not carried over.
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_usable_restart_records_an_unrecorded_failure_before_the_boundary(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )
    compose.fail = False

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert len(await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)) == 1
    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert [event.payload for event in recovered] == [{"previous_consecutive_failures": 1}]
    assert await _streak(factory, ws_id) == 0
    assert executor._restart_streak_unrecorded_failures == {}


@pytest.mark.unit
async def test_unrecorded_failure_survives_a_failed_recovery_write(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure held for a ``recovered`` write that fails is retried ahead of it."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []

    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == 1
    assert failed[0].reason_code == "MONITOR_RECOVERY_COMPOSE_FAILED"
    recovered = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE)
    assert [event.payload for event in recovered] == [{"previous_consecutive_failures": 1}]
    assert failed[0].event_order < recovered[0].event_order
    assert await _streak(factory, ws_id) == 0
    assert executor._restart_streak_unrecorded_failures == {}
    assert executor._restart_streak_recovery_failures == {}


@pytest.mark.unit
async def test_unrecorded_failure_retry_error_keeps_it_in_memory(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    pending = list(executor._restart_streak_unrecorded_failures[ws_id])

    class _DownRepo(WorkspaceRepository):
        async def add_events(self, *_args: Any, **_kwargs: Any) -> Any:
            raise OperationalError("insert", {}, Exception("connection reset"))

    monkeypatch.setattr(monitor_resume_restart_escalation, "WorkspaceRepository", _DownRepo)
    await monitor_resume_restart_escalation._persist_unrecorded_failures(
        executor, workspace_id=ws_id
    )

    assert executor._restart_streak_unrecorded_failures[ws_id] == pending
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_unrecorded_failure_is_dropped_once_the_workspace_left_monitoring(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.status = WorkspaceStatus.cancelled.value
        await session.commit()

    await monitor_resume_restart_escalation._persist_unrecorded_failures(
        executor, workspace_id=ws_id
    )

    assert executor._restart_streak_unrecorded_failures == {}
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_unrecorded_failure_after_an_unrecorded_recovery_still_counts(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late ``recovered`` boundary must not close a failure from after the restart."""
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _fail_the_next_streak_count(monkeypatch)
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None
    compose.fail = True
    # The recovery retry and the next failure's write both fail.
    _fail_the_next_streak_count(monkeypatch)
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 2):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.monitoring_pr.value
    assert await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE) == []

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["consecutive_failures"] == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT


@pytest.mark.unit
async def test_park_on_an_unrecorded_timeout_keeps_its_unsettled_dependencies(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guided retry still recreates what the unrecorded parking timeout left wedged."""
    (tmp_path / "compose.yml").write_text(_POSTGRES_COMPOSE, encoding="utf-8")
    ws_id = await _seed(factory, tmp_path)
    compose = _ScriptedCompose()
    executor = _executor(fake, factory, tmp_path, compose)
    for _ in range(MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1):
        assert await executor.resume_pr_monitor_handoff(ws_id) is None
    compose.fail = False
    _dependency_health(monkeypatch, {"postgres": "unhealthy"})
    _fail_failure_event_writes(monkeypatch, times=1)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert await _status(factory, ws_id) == WorkspaceStatus.blocked.value
    failed = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE)
    assert len(failed) == MONITOR_RUNTIME_RESTART_FAILURE_LIMIT - 1
    assert all(
        event.payload is not None and "unsettled_dependencies" not in event.payload
        for event in failed
    )
    exhausted = await _events(factory, ws_id, MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE)
    assert exhausted[0].payload is not None
    assert exhausted[0].payload["pending_dependency_recreates"] == ["postgres"]
    assert executor._restart_streak_unrecorded_failures == {}

    await _guide(factory, ws_id)
    compose.calls.clear()
    _dependency_health(monkeypatch, {"postgres": "healthy"})
    assert await executor.resume_pr_monitor_handoff(ws_id) is not None

    assert compose.calls[0]["services"] == ("postgres",)
    assert compose.calls[0]["force_recreate"] is True
