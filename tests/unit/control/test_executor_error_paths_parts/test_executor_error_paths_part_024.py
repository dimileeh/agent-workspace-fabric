"""Monitor-recovery _mark_failed must clear HUMAN_WAIT attention (PR #805)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_SOURCE_MONITORING_PR,
)
from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _make_executor,
    _seed_monitoring_pr,
    factory,
    fake,
)

_IMPORTED_FIXTURES = (factory, fake)


@pytest.mark.unit
async def test_resume_mark_failed_clears_persisted_human_attention(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6XdyUu: a persisted HUMAN_WAIT episode that survives a
    control-plane restart must not stay open when ``resume_pr_monitor_handoff``
    fails before the monitor runner starts (e.g. missing recovery metadata).

    That path calls the executor's ``_mark_failed(..., from_status=monitoring_pr)``
    rather than ``PRMonitorRunner._terminate_failed``, so the fail transition
    must clear attention and emit ``workspace.attention_cleared`` itself —
    otherwise timeline consumers see a permanently open episode on a failed row.
    """
    ws_id = await _seed_monitoring_pr(factory, pr_number=None)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        await repo.set_workspace_attention(
            ws_id,
            reason="notify human: episode survived restart",
            now=datetime(2026, 8, 8, 11, 0, tzinfo=UTC),
        )
        await session.commit()
        ws = await repo.get(ws_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert ws.awaiting_human_reason is not None

    def _monitor_factory(*_args: Any) -> object:
        raise AssertionError("monitor factory must not run for invalid recovery rows")

    executor = _make_executor(fake, factory, tmp_path, pr_monitor_factory=_monitor_factory)

    await executor.resume_pr_monitor(ws_id)

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert any(event.reason_code == "MONITOR_RECOVERY_METADATA_MISSING" for event in ws.events)
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None
        cleared = [
            event
            for event in ws.events
            if event.event_type == ATTENTION_CLEARED_EVENT_TYPE
            and (event.payload or {}).get("source") == ATTENTION_SOURCE_MONITORING_PR
        ]
    assert len(cleared) == 1
