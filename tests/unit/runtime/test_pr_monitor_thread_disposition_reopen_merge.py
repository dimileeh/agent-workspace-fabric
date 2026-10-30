"""Feature-monitor merge path of the automatic disposition reopen (#1138).

The merge loop audits review threads before it takes the merge lock and again
under it. A thread either audit flags unverified is reopened by AWF itself,
below the per-thread cap, and the monitor re-polls instead of merging. Nothing
reopens or sleeps while the merge lock is held: the in-lock re-audit's result is
acted on only after the lock is released.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import ReviewThreadAudit
from awf.db.enums import WorkspaceStatus
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _disposition_auto_reopen_key
from tests.postgres import postgres_test_engine
from tests.unit.runtime._thread_disposition_fixtures import settled_baseline_state
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _SILENT,
    _VIEWER,
    _attention,
    _thread,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate_merge import (
    _LockLog,
    _merge_under_lock_log,
    _SequencedAuditMergeClient,
)

pytestmark = pytest.mark.unit

_CLEAN = ReviewThreadAudit(supported=True, viewer_login=_VIEWER)
_UNVERIFIED = ReviewThreadAudit(
    supported=True, viewer_login=_VIEWER, threads=(_thread(_SILENT, external=("P1",)),)
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _ReopenMergeClient(_SequencedAuditMergeClient):
    """Sequenced audits; each reopen records whether the merge lock was held."""

    def __init__(self, lock: _LockLog, *results: ReviewThreadAudit | ForgeClientError) -> None:
        super().__init__(*results)
        self.unresolve = True
        self.lock = lock
        self.reopens_under_lock: list[bool] = []

    async def unresolve_thread(self, *, thread_id: str) -> bool:
        self.reopens_under_lock.append(self.lock.held)
        return await super().unresolve_thread(thread_id=thread_id)


async def test_pre_lock_unverified_thread_is_reopened_instead_of_blocking_the_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    lock = _LockLog()
    gh = _ReopenMergeClient(lock, _UNVERIFIED)

    terminal, state, _, workspace_id = await _merge_under_lock_log(factory, tmp_path, gh, lock=lock)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0]  # never reached the lock
    assert gh.unresolved_threads == [_SILENT]
    assert gh.reopens_under_lock == [False]
    assert lock.sleeps_under_lock == [False]
    assert gh.merge_calls == [] and gh.comments == []
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"
    since, _, status = await _attention(factory, workspace_id)
    assert since is None and status == WorkspaceStatus.monitoring_pr.value


async def test_in_lock_unverified_thread_is_reopened_only_after_the_lock_is_released(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    lock = _LockLog()
    gh = _ReopenMergeClient(lock, _CLEAN, _UNVERIFIED)

    terminal, state, _, _ = await _merge_under_lock_log(factory, tmp_path, gh, lock=lock)

    assert terminal is False
    assert gh.status_refreshes_at_audit == [0, 1]  # the in-lock re-audit flagged it
    assert gh.unresolved_threads == [_SILENT]
    assert gh.reopens_under_lock == [False]
    assert lock.sleeps_under_lock == [False]
    assert gh.merge_calls == [] and gh.comments == []
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_SILENT)] == "1"


async def test_in_lock_unverified_thread_over_the_cap_posts_todays_blocker_after_release(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    lock = _LockLog()
    gh = _ReopenMergeClient(lock, _CLEAN, _UNVERIFIED)
    state = settled_baseline_state()
    state.mark_addressed(_disposition_auto_reopen_key(_SILENT), "2")

    terminal, _, _, workspace_id = await _merge_under_lock_log(
        factory, tmp_path, gh, state=state, lock=lock
    )

    assert terminal is False
    assert gh.unresolved_threads == []
    assert gh.merge_calls == []
    assert len(gh.comments) == 1
    assert "REVIEW_THREAD_DISPOSITION_UNVERIFIED" in gh.comments[0]
    assert "REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED" in gh.comments[0]
    assert True not in lock.sleeps_under_lock
    _, _, status = await _attention(factory, workspace_id)
    assert status == WorkspaceStatus.monitoring_pr.value
