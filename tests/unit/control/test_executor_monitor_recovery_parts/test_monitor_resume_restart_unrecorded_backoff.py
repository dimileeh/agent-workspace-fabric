"""Unrecorded restart failures still pace the next restart (#936).

A failure event whose write and retry both failed still counts toward the
streak, so it must size and time the backoff too; otherwise the next restarts
run back to back and the workspace parks without the configured gaps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import monitor_resume_restart_escalation
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY,
)
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_monitor_recovery_parts.test_monitor_resume_restart_escalation import (  # noqa: E501
    _executor,
    _fail_failure_event_writes,
    _noop_persist,
    _record_backoff,
    _runtime_unusable,
    _ScriptedCompose,
    _seed,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (_runtime_unusable, factory, fake)


@pytest.mark.unit
@pytest.mark.parametrize("recorded_failures", [0, 1])
async def test_unrecorded_failure_sizes_and_times_the_backoff(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_failures: int,
) -> None:
    """Reads still work, so the pending failure counts and its time starts the gap."""
    ws_id = await _seed(factory, tmp_path)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        for _ in range(recorded_failures):
            stored = await repo.add_event(
                ws,
                event_type=MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
                reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
                payload={MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY: True},
            )
            # Long past its own backoff, so only the unrecorded failure can hold.
            stored.occurred_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
    executor = _executor(fake, factory, tmp_path, _ScriptedCompose())
    _fail_failure_event_writes(monkeypatch, times=1)
    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert executor._restart_streak_unrecorded_failures[ws_id]
    monkeypatch.setattr(
        monitor_resume_restart_escalation, "_persist_unrecorded_failures", _noop_persist
    )
    sleeps = _record_backoff(monkeypatch, clock_offset=timedelta(0))

    await monitor_resume_restart_escalation.pace_monitor_resume_restart(
        executor, workspace_id=ws_id
    )

    assert sleeps == [
        pytest.approx(MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS[recorded_failures], abs=5)
    ]
