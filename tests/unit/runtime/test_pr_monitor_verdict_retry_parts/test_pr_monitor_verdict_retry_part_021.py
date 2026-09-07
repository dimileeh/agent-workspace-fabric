"""The preserved-timeout anchor must outlive the worker, not just the cycle (#934).

Both #932 preserve entries — ``handle_agent_run_error`` and
``preserve_timeout_work_and_raise_cleanup_error`` — keep the timed-out agent's
commits on disk and remember the item's original start HEAD so the retry can still
count them as this item's own ``FIXED`` evidence. That marker used to live only in
the in-memory ``MonitorState``, which reaches the DB solely through ``run()``'s
post-``_execute`` ``_persist_state``. A worker cancelled or killed in between —
exactly the window this path is built for — leaves the salvaged commits on disk
with no anchor, so the retry anchors at the *preserved* HEAD and rejects an honest
no-change ``FIXED`` as ``AGENT_FIXED_WITHOUT_EVIDENCE`` (PRRT_kwDOSJAM6s6fzBXj).

These tests pin the durable write: the anchor is on the workspace row before the
first cancellable step of the preserve sequence, and a DB fault there degrades to
today's in-memory behaviour instead of replacing the timeout's reason code.

The same durability is owed to an anchor an item only *earns* mid-run. When the
service-recovery loop preserves a watchdog timeout and then gives up, the #932
preserve handler never sees that timeout, so the protocol publishes the owed
anchor itself — and the recovery-failed exit in ``run()`` returns without
``_persist_state``, so an in-memory re-arm alone dies with the cycle
(PRRT_kwDOSJAM6s6f0ft2).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    remember_item_start_head_durably,
)
from awf.runtime.pr_monitor_runner.types import _MonitorAgentServiceRecoveryFailedError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"
_BODY_HASH = "feedbackhash"
_TIMEOUT_ENTRIES = ("agent_run_error", "cleanup_failure")


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=1, stdout="", stderr="watchdog fired"),
        reason_code="AGENT_TIMEOUT",
    )


def _cleanup_error() -> ComposeExecCleanupError:
    exc = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="tagged process still alive",
    )
    exc.agent_reason_code = "AGENT_TIMEOUT"
    return exc


def _runner(
    tmp_path: Path,
    *,
    workspace_id: str,
    entry: str,
    session_factory: object,
) -> _VerdictRunner:
    """An agent that self-commits and dirties the tree, then hits a watchdog timeout."""
    (tmp_path / workspace_id).mkdir(parents=True)
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error()],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    runner._deps.session_factory = session_factory
    if entry == "cleanup_failure":
        # The adapter tears the exec stack down before raising the agent's own
        # error, so a timed-out run whose cleanup also failed arrives as a
        # ``ComposeExecCleanupError`` carrying the watchdog classification.
        exc = _cleanup_error()

        async def _run(**kwargs: object) -> None:
            runner.prompts.append(str(kwargs["prompt"]))
            runner.attempt += 1
            runner.current_head = _PRESERVED_HEAD
            runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
            raise exc

        runner._run_monitor_agent_with_service_recovery = _run
    return runner


async def _invoke_item(
    runner: _VerdictRunner,
    *,
    workspace_id: str,
    state: MonitorState,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project=f"awf_{workspace_id}",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_id=_ITEM_ID,
        evidence_body_hash=_BODY_HASH,
    )


async def _persisted_anchor(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str | None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return (ws.monitor_threads_addressed or {}).get(item_start_head_state_key(_ITEM_ID))


@pytest.mark.unit
@pytest.mark.parametrize("entry", _TIMEOUT_ENTRIES)
async def test_preserved_anchor_is_on_the_row_before_the_worker_can_die(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    entry: str,
) -> None:
    """Cancel inside the preserve sink: the anchor is already durable, bound to the body."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(tmp_path, workspace_id=workspace_id, entry=entry, session_factory=factory)

    async def _cancelled_sink(**_kwargs: object) -> bool:
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _cancelled_sink
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, workspace_id=workspace_id, state=state)

    # The cancellation never reaches ``_persist_state``, so only the durable write
    # can carry the anchor across the restart.
    assert await _persisted_anchor(factory, workspace_id) == f"{_BODY_HASH}:{_ITEM_START_HEAD}"
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("entry", _TIMEOUT_ENTRIES)
async def test_durable_anchor_write_failure_keeps_the_timeout_reason_code(
    tmp_path: Path,
    entry: str,
) -> None:
    """A DB fault degrades to the in-memory marker; it never becomes the outcome."""
    workspace_id = "ws_anchor_db_fault"

    def _broken_factory() -> object:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    runner = _runner(
        tmp_path,
        workspace_id=workspace_id,
        entry=entry,
        session_factory=_broken_factory,
    )
    state = MonitorState()

    expected: type[BaseException] = (
        ComposeExecCleanupError
        if entry == "cleanup_failure"
        else comment_verdict.AgentVerdictExecutionError
    )
    with structlog.testing.capture_logs() as captured, pytest.raises(expected) as raised:
        await _invoke_item(runner, workspace_id=workspace_id, state=state)

    if entry == "agent_run_error":
        assert raised.value.reason_code == "AGENT_TIMEOUT"  # type: ignore[attr-defined]
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == (
        f"{_BODY_HASH}:{_ITEM_START_HEAD}"
    )
    assert runner.reset_targets == []
    failures = [
        entry_log
        for entry_log in captured
        if entry_log.get("event") == "monitor.agent_verdict_item_start_head_durable_write_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["item_start_head"] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_durable_anchor_write_skips_a_workspace_row_that_is_gone(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A destroyed workspace has no row to anchor; the preserve path still returns."""
    state = MonitorState()

    await remember_item_start_head_durably(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=factory)),  # type: ignore[arg-type]
        workspace_id="ws_never_existed",
        state=state,
        item_id=_ITEM_ID,
        head=_ITEM_START_HEAD,
        body_hash=_BODY_HASH,
    )

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == (
        f"{_BODY_HASH}:{_ITEM_START_HEAD}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("item_id", "head"),
    [(None, _ITEM_START_HEAD), (_ITEM_ID, None)],
)
async def test_durable_anchor_write_needs_both_an_item_and_a_head(
    item_id: str | None,
    head: str | None,
) -> None:
    """Nothing to anchor: no session is opened at all."""
    opened: list[str] = []

    def _tracking_factory() -> object:
        opened.append("session")
        raise AssertionError("no session should be opened")

    await remember_item_start_head_durably(
        SimpleNamespace(_deps=SimpleNamespace(session_factory=_tracking_factory)),  # type: ignore[arg-type]
        workspace_id="ws_partial_anchor",
        state=MonitorState(),
        item_id=item_id,
        head=head,
    )

    assert opened == []


def _recovery_failed_after_rerun_runner(
    tmp_path: Path,
    *,
    workspace_id: str,
    session_factory: object,
) -> _VerdictRunner:
    """A timeout the recovery loop preserved, then gave the recovery up on.

    The loop publishes the floor it would have rerun over — so the item earns an
    evidence anchor it never had on entry — and raises the recovery failure.
    """
    (tmp_path / workspace_id).mkdir(parents=True)
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[False],
    )
    runner.current_head = _ITEM_START_HEAD
    runner._deps.session_factory = session_factory

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        # The timed-out run self-committed before the watchdog fired.
        runner.current_head = _PRESERVED_HEAD
        floor_sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(floor_sink, list)
        floor_sink.append(_PRESERVED_HEAD)
        preservation_sink = kwargs["timeout_preservation_sink"]
        assert isinstance(preservation_sink, list)
        preservation_sink.append("AGENT_TIMEOUT")
        raise _MonitorAgentServiceRecoveryFailedError("agent service never came back")

    runner._run_monitor_agent_with_service_recovery = _run
    return runner


@pytest.mark.unit
async def test_an_anchor_earned_mid_run_is_durable_before_the_recovery_failure_exit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The mid-run anchor survives the one exit that never persists the state.

    ``run()``'s ``_MonitorAgentServiceRecoveryFailedError`` arm returns without
    ``_persist_state``, so the in-memory re-arm dies with the cycle while the
    timed-out commit stays on disk — and the next invocation anchors the item at
    that preserved HEAD, rejecting an honest no-change ``FIXED`` as
    ``AGENT_FIXED_WITHOUT_EVIDENCE`` (PRRT_kwDOSJAM6s6f0ft2).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _recovery_failed_after_rerun_runner(
        tmp_path,
        workspace_id=workspace_id,
        session_factory=factory,
    )
    state = MonitorState()

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await _invoke_item(runner, workspace_id=workspace_id, state=state)

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == (
        f"{_BODY_HASH}:{_ITEM_START_HEAD}"
    )
    assert await _persisted_anchor(factory, workspace_id) == f"{_BODY_HASH}:{_ITEM_START_HEAD}"
    # The preserved commit itself is untouched: the rollback stops at the floor.
    assert runner.current_head == _PRESERVED_HEAD
