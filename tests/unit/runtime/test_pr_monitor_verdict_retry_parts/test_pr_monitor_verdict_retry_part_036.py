"""A baseline-outage park persists only its stranded-edit keys (PRRT_kwDOSJAM6s63UyV-).

The park's preservation runs inside a review batch whose earlier items can carry
``fix_committed`` markers for commits not yet pushed or resolved; the batch rolls
them back only after the outage propagates. The preservation's write therefore
merges just the stranded-edit keys onto the workspace row, so a cancellation that
skips that rollback cannot reload those items as addressed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
from awf.runtime.pr_monitor_runner.comment_verdict_baseline_park import (
    persist_stranded_edits_marker,
)
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY,
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_REPAIR_HEAD = "b" * 40
_EVIDENCE = '{"fix.py": "sha"}'


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _runner(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> PullRequestMonitorRunner:
    return make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )


def _parked_batch_state() -> MonitorState:
    """A park's marker beside an earlier batch item's unpushed fix."""
    state = MonitorState()
    state.threads_addressed_ids["PRRT_earlier_unpushed"] = "fix_committed"
    state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] = _REPAIR_HEAD
    state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY] = _EVIDENCE
    return state


async def _seed_row(
    factory: async_sessionmaker[AsyncSession], threads_addressed: dict[str, str]
) -> str:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = threads_addressed
        await s.commit()
    return workspace_id


async def _row(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> dict[str, str]:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        return dict(ws.monitor_threads_addressed or {})


@pytest.mark.unit
async def test_park_persist_writes_only_stranded_edit_keys(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = await _seed_row(
        factory,
        {
            "PRRT_already_resolved": "fix_committed",
            PROVIDER_PARK_STRANDED_EDITS_OWNED_STATE_KEY: '["stale.py"]',
        },
    )

    await persist_stranded_edits_marker(
        _runner(factory, tmp_path), workspace_id=workspace_id, state=_parked_batch_state()
    )

    assert await _row(factory, workspace_id) == {
        "PRRT_already_resolved": "fix_committed",
        PROVIDER_PARK_STRANDED_EDITS_STATE_KEY: _REPAIR_HEAD,
        PROVIDER_PARK_STRANDED_EDITS_EVIDENCE_STATE_KEY: _EVIDENCE,
    }


@pytest.mark.unit
async def test_park_persist_missing_workspace_is_noop(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await persist_stranded_edits_marker(
        _runner(factory, tmp_path), workspace_id="ws_does_not_exist", state=_parked_batch_state()
    )


@pytest.mark.unit
async def test_park_persist_from_superseded_owner_is_fenced(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    workspace_id = await _seed_row(factory, {})
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-new"
        await s.commit()
    runner = _runner(factory, tmp_path)
    runner._monitor_owner_id = "worker-old"
    state = _parked_batch_state()

    await persist_stranded_edits_marker(runner, workspace_id=workspace_id, state=state)
    await persist_stranded_edits_marker(runner, workspace_id=workspace_id, state=state)

    assert state.monitor_writes_suppressed is True
    assert await _row(factory, workspace_id) == {}
