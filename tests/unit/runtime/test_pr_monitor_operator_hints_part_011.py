"""The operator-hint timeout retry budget must outlive the worker (#934).

The #932 retry leaves the hint durably ``pending`` in ``monitor_threads_addressed``
so ``decide()`` re-issues ``AddressOperatorHint``, but the marker that makes that
retry *single* used to live only in the in-memory ``MonitorState``, which reaches
the DB solely through ``run()``'s post-``_execute`` ``_persist_state``. A worker
killed in between — a shutdown cancellation, a crash, a ``_finish_monitor_operation``
fault — resumed against a pending hint with no budget and granted the "single"
retry again, so a hint that kept timing out in that window re-ran the agent instead
of escalating to a human (PRRT_kwDOSJAM6s6fzBXq).

These tests pin the durable write: the marker is on the workspace row by the time
the retry envelope is returned, and a DB fault there degrades to the previous
in-memory behaviour instead of replacing the timeout's reason code. The write also
has to survive the very shutdown it defends against — a cancellation landing on its
own transaction must not roll the marker back (PRRT_kwDOSJAM6s6f8cgf).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace, TracebackType

import pytest
import structlog
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.operator_hint_timeout_retry import (
    mark_timeout_retry_used_durably,
    operator_hint_timeout_retry_key,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_PRESERVED_HEAD = "b" * 40
_OPERATION_ID = "op_timed_out_hint_durable"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _hint() -> OperatorHint:
    return OperatorHint(
        reason="the fix-cycle stalled; finish the refactor",
        directive="finish the refactor and commit it",
        operation_id=_OPERATION_ID,
        requested_at="2026-09-06T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


async def _persisted_marker(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    hint: OperatorHint,
) -> str | None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return (ws.monitor_threads_addressed or {}).get(operator_hint_timeout_retry_key(hint))


@pytest.mark.unit
async def test_timeout_retry_marker_is_on_the_row_before_the_worker_can_die(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cycle returns the retry envelope with the budget already durable."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("a" * 40, None)

    async def _timed_out(**_kwargs: object) -> object:
        raise AgentVerdictExecutionError(
            reason_code="AGENT_IDLE_TIMEOUT",
            reason=f"agent timed out; preserved work at {_PRESERVED_HEAD}",
            preserved_head_sha=_PRESERVED_HEAD,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _timed_out)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.operator_hint_timeout_retry is True
    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"
    # Nothing flushed ``MonitorState`` here, so only the durable write can carry the
    # spent budget across a restart — without it the next resume retries again.
    assert await _persisted_marker(factory, workspace_id, hint) == "retried"


@pytest.mark.unit
async def test_durable_marker_write_failure_keeps_the_in_memory_budget() -> None:
    """A DB fault degrades to the in-memory marker and only logs."""
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    def _broken_factory() -> object:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with structlog.testing.capture_logs() as captured:
        await mark_timeout_retry_used_durably(
            SimpleNamespace(_deps=SimpleNamespace(session_factory=_broken_factory)),  # type: ignore[arg-type]
            workspace_id="ws_retry_db_fault",
            state=state,
            hint=hint,
        )

    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"
    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.operator_hint_timeout_retry_durable_write_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["operation_id"] == _OPERATION_ID


@pytest.mark.unit
async def test_unexpected_write_failure_degrades_instead_of_raising() -> None:
    """A fault the write itself does not classify still must not escape this helper.

    ``_write_timeout_retry_marker`` degrades on ``SQLAlchemyError``/``OSError``; anything
    else used to reach ``write_task.result()`` and propagate out of the retry branch, so
    the caller never returned the ``operator_hint_timeout_retry`` envelope and a storage
    error replaced the watchdog reason code (PRRT_kwDOSJAM6s6f_brh).
    """
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    def _broken_factory() -> object:
        raise RuntimeError("session factory is gone")

    with structlog.testing.capture_logs() as captured:
        await mark_timeout_retry_used_durably(
            SimpleNamespace(_deps=SimpleNamespace(session_factory=_broken_factory)),  # type: ignore[arg-type]
            workspace_id="ws_retry_unexpected_fault",
            state=state,
            hint=hint,
        )

    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"
    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.operator_hint_timeout_retry_durable_write_failed"
    ]
    assert len(failures) == 1
    assert "session factory is gone" in failures[0]["error"]


@pytest.mark.unit
async def test_durable_marker_write_skips_a_workspace_row_that_is_gone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A destroyed workspace has no row to mark; the retry path still returns."""
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    await mark_timeout_retry_used_durably(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),  # type: ignore[arg-type]
        workspace_id="ws_never_existed",
        state=state,
        hint=hint,
    )

    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"


class _GatedSession:
    """Proxy an ``AsyncSession`` whose ``commit`` waits for the test to release it."""

    def __init__(self, inner: AsyncSession, ready: asyncio.Event, release: asyncio.Event) -> None:
        self._inner = inner
        self._ready = ready
        self._release = release

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def commit(self) -> None:
        self._ready.set()
        await self._release.wait()
        await self._inner.commit()


class _FactoryFailingOutsideSQLAlchemy:
    """A session factory whose failure is *not* one the write degrades on."""

    def __init__(self, ready: asyncio.Event, release: asyncio.Event) -> None:
        self._ready = ready
        self._release = release

    def __call__(self) -> _FactoryFailingOutsideSQLAlchemy:
        return self

    async def __aenter__(self) -> AsyncSession:
        self._ready.set()
        await self._release.wait()
        raise RuntimeError("session factory is gone")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


@pytest.mark.unit
async def test_worker_cancellation_mid_write_still_lands_the_marker(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A shutdown landing on the write's own transaction must not roll the marker back."""
    workspace_id = await seed_monitoring_workspace(factory)
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    ready, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def _gated_factory() -> AsyncIterator[_GatedSession]:
        async with factory() as session:
            yield _GatedSession(session, ready, release)

    task = asyncio.ensure_future(
        mark_timeout_retry_used_durably(
            SimpleNamespace(_deps=SimpleNamespace(session_factory=_gated_factory)),  # type: ignore[arg-type]
            workspace_id=workspace_id,
            state=state,
            hint=hint,
        )
    )
    await ready.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    # Unshielded, the cancellation aborted the transaction and the next worker
    # granted the "single" retry all over again.
    assert await _persisted_marker(factory, workspace_id, hint) == "retried"


@pytest.mark.unit
async def test_write_failure_dropped_by_the_shield_is_logged_not_raised() -> None:
    """A failure the shield swallows still gets its event, and the shutdown wins."""
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    ready, release = asyncio.Event(), asyncio.Event()
    session_factory = _FactoryFailingOutsideSQLAlchemy(ready, release)

    with structlog.testing.capture_logs() as captured:
        task = asyncio.ensure_future(
            mark_timeout_retry_used_durably(
                SimpleNamespace(_deps=SimpleNamespace(session_factory=session_factory)),  # type: ignore[arg-type]
                workspace_id="ws_retry_cancelled_write",
                state=state,
                hint=hint,
            )
        )
        await ready.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.operator_hint_timeout_retry_durable_write_failed"
    ]
    assert len(failures) == 1
    assert "session factory is gone" in failures[0]["error"]
    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"


@pytest.mark.unit
async def test_durable_marker_write_without_a_session_factory_is_in_memory_only() -> None:
    """A runner with no session factory (unit harnesses) keeps today's behaviour."""
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    await mark_timeout_retry_used_durably(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=None)),  # type: ignore[arg-type]
        workspace_id="ws_no_session_factory",
        state=state,
        hint=hint,
    )

    assert state.threads_addressed_ids[operator_hint_timeout_retry_key(hint)] == "retried"
