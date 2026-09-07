"""The recovery loop's cleanup-error branch owes the same floor bookkeeping (#934).

The adapter tears its exec stack down before re-raising the agent's own error, so
a timed-out run whose cleanup also failed reaches
``_run_monitor_agent_with_service_recovery_locked`` as a
``ComposeExecCleanupError`` carrying the watchdog classification rather than as an
``AgentRunError``. That branch restarts the service and reruns exactly like the
``AgentRunError`` branch covered in the base module, so it owes the same
pre-rerun floor publication, dirty-worktree sink, preservation-sink claim, and
carry of the masked timeout into the rerun's pre-launch guards.

Split out of ``tests/unit/runtime/test_agent_service_recovery_timeout_rerun_floor.py``
to keep each test module under the first-party 1500-line maintainability
guardrail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunError
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from tests.unit.runtime.test_agent_service_recovery_timeout_rerun_floor import (
    _PRE_RERUN_HEAD,
    _SUNK_HEAD,
    _WORKSPACE_ID,
    _CleanupErrorThenOkAdapter,
    _RecoveryRunner,
    _run_locked,
    _stub_cleanup_recovery,
    _stub_recovery,
)


@pytest.mark.unit
async def test_a_recovered_cleanup_failure_masking_a_timeout_preserves_that_runs_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout whose cleanup also failed still loses its run to the rerun.

    The adapter tears the exec stack down before raising the agent's own error,
    so a timed-out run whose cleanup fails reaches this loop as a
    ``ComposeExecCleanupError`` carrying the watchdog classification. The
    cleanup-recovery branch restarts the service and reruns exactly like the
    ``AgentRunError`` branch, so it owes the same preservation bookkeeping:
    without it a provider failure or non-FIXED verdict on the rerun rewinds to
    the attempt start and deletes the timed-out run's commits and edits
    (PRRT_kwDOSJAM6s6fvw8t).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_IDLE_TIMEOUT")
    )
    calls: list[str] = []
    sunk_reason_codes: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        calls.append("sink")
        sunk_reason_codes.append(reason_code)
        runner._head = _SUNK_HEAD
        return True

    async def _head(worktree_path: Path) -> str | None:
        calls.append("head")
        runner.head_reads.append(worktree_path)
        return runner._head

    runner._rev_parse_head = _head  # type: ignore[method-assign]
    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink, _dirty_sink)

    assert result.returncode == 0
    assert runner.runs == 2
    assert calls == ["sink", "head"]
    assert sunk_reason_codes == ["AGENT_IDLE_TIMEOUT"]
    assert sink == [_SUNK_HEAD]


@pytest.mark.unit
async def test_a_recovered_cleanup_failure_without_a_timeout_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure that masks no watchdog timeout has no #932 work to keep."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code=None)
    )
    sink_calls: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sink_calls.append(reason_code)
        return False

    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink, _dirty_sink)

    assert result.returncode == 0
    assert runner.runs == 2
    assert sink_calls == []
    assert sink == []


@pytest.mark.unit
async def test_an_unrecovered_cleanup_failure_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rerun means the cleanup error reaches the caller's own preserve handler."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    sink_calls: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sink_calls.append(reason_code)
        return False

    _stub_cleanup_recovery(monkeypatch, recovered=None)
    sink: list[str] = []

    with pytest.raises(ComposeExecCleanupError):
        await _run_locked(runner, sink, _dirty_sink)

    assert sink_calls == []
    assert sink == []


def _stub_raising_cleanup_recovery(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        raise exc

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_cleanup_error",
        _recover,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovery_exc",
    [
        _MonitorAgentServiceRecoveryFailedError("restart failed"),
        _MonitorAgentServiceRecoverySupersededError("claim changed"),
        _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object gone"),
        _MonitorMirrorHooksPathRepairFailedError(),
    ],
    ids=["restart_failed", "superseded", "head_object_missing", "mirror_hooks"],
)
async def test_a_masked_timeout_is_preserved_when_cleanup_recovery_gives_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_exc: BaseException,
) -> None:
    """Abandoned cleanup recovery must not cost the timed-out run its work either.

    ``_recover_monitor_agent_service_after_cleanup_error`` restarts the service
    and repairs Git before it gives up, so these exits leave the timed-out run's
    commits and edits in the worktree — and every one of them lands in a caller
    handler that rolls back to the floor before propagating. Publishing the
    preservation bookkeeping first is what keeps that rollback off the work #932
    promised to keep (PRRT_kwDOSJAM6s6fvw8t).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    sunk_reason_codes: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sunk_reason_codes.append(reason_code)
        runner._head = _SUNK_HEAD
        return True

    _stub_raising_cleanup_recovery(monkeypatch, recovery_exc)
    sink: list[str] = []

    with pytest.raises(type(recovery_exc)):
        await _run_locked(runner, sink, _dirty_sink)

    assert runner.runs == 1
    assert sunk_reason_codes == ["AGENT_TIMEOUT"]
    assert sink == [_SUNK_HEAD]


@pytest.mark.unit
@pytest.mark.parametrize(
    "recovery_exc",
    [
        _MonitorAgentServiceRecoveryFailedError("restart failed"),
        _MonitorAgentServiceRecoverySupersededError("claim changed"),
        _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING", "head object gone"),
        _MonitorMirrorHooksPathRepairFailedError(),
    ],
    ids=["restart_failed", "superseded", "head_object_missing", "mirror_hooks"],
)
async def test_a_stranded_salvage_escalates_the_tagged_cleanup_error_instead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_exc: BaseException,
) -> None:
    """An abandoned recovery must not roll back over edits the salvage stranded.

    The published floor is only a SHA, so the caller's service-recovery handler
    resets to it and cleans the worktree — deleting the timed-out run's still
    dirty edits. The give-up branch therefore checks the preservation result just
    like the rerun branches: it escalates the timeout-tagged cleanup error, whose
    caller handler preserves the work instead (PRRT_kwDOSJAM6s6fyEEr).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    sunk_reason_codes: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sunk_reason_codes.append(reason_code)
        return False

    _stub_raising_cleanup_recovery(monkeypatch, recovery_exc)
    sink: list[str] = []

    with pytest.raises(ComposeExecCleanupError) as caught:
        await _run_locked(runner, sink, _dirty_sink)

    assert caught.value.__cause__ is recovery_exc
    assert runner.runs == 1
    assert sunk_reason_codes == ["AGENT_TIMEOUT"]
    # The commits are still covered by the published floor; the stranded edits
    # are what the escalated cleanup error keeps out of the rollback.
    assert sink == [_PRE_RERUN_HEAD]


@pytest.mark.unit
async def test_an_unpublishable_floor_escalates_the_tagged_cleanup_error_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no floor published the caller's stays at the attempt start.

    Propagating the recovery exception there resets straight through the
    timed-out run's commits, so the tagged cleanup error is escalated instead
    (PRRT_kwDOSJAM6s6fyEEr).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=None)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    recovery_exc = _MonitorAgentServiceRecoveryFailedError("restart failed")

    async def _dirty_sink(_reason_code: str) -> bool:
        return True

    _stub_raising_cleanup_recovery(monkeypatch, recovery_exc)
    sink: list[str] = []

    with pytest.raises(ComposeExecCleanupError) as caught:
        await _run_locked(runner, sink, _dirty_sink)

    assert caught.value.__cause__ is recovery_exc
    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_an_abandoned_cleanup_recovery_without_a_timeout_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exit that masks no watchdog timeout keeps the caller's rollback intact."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code=None)
    )
    sink_calls: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sink_calls.append(reason_code)
        return True

    _stub_raising_cleanup_recovery(
        monkeypatch,
        _MonitorAgentServiceRecoveryFailedError("restart failed"),
    )
    sink: list[str] = []

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await _run_locked(runner, sink, _dirty_sink)

    assert sink_calls == []
    assert sink == []


@pytest.mark.unit
async def test_callers_that_pass_no_sink_are_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other monitor caller keeps the pre-existing signature and behaviour."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    _stub_recovery(monkeypatch, recovered=1)

    result = await _run_locked(runner, None)

    assert result.returncode == 0
    assert runner.head_reads == []


@pytest.mark.unit
async def test_cancellation_inside_the_dirty_sink_marks_the_work_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The salvage await runs before either protection channel is populated.

    ``CancelledError`` is a ``BaseException``, so it escapes this bookkeeping with
    the floor sink still empty — the caller then never raises its rollback floor
    and its cancellation branch rewinds to the attempt start, deleting the
    timed-out run's commits and the salvage commit the sink may just have made.
    The preservation sink must therefore be marked before the first await
    (PRRT_kwDOSJAM6s6fy7ju).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _cancelled_sink(_reason_code: str) -> bool:
        raise asyncio.CancelledError()

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    with pytest.raises(asyncio.CancelledError):
        await _run_locked(runner, sink, _cancelled_sink, preservation_sink)

    assert runner.runs == 1
    assert sink == []
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]


@pytest.mark.unit
async def test_cancellation_inside_the_floor_probe_marks_the_work_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HEAD probe after the sink is cancellable too, and just as unprotected."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _cancelled_head(_worktree_path: Path) -> str | None:
        raise asyncio.CancelledError()

    runner._rev_parse_head = _cancelled_head  # type: ignore[method-assign]
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    with pytest.raises(asyncio.CancelledError):
        await _run_locked(runner, sink, None, preservation_sink)

    assert sink == []
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]


@pytest.mark.unit
async def test_cancellation_inside_the_cleanup_branch_sink_marks_the_work_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup-recovery branch does the same bookkeeping, so it needs the same mark."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )

    async def _cancelled_sink(_reason_code: str) -> bool:
        raise asyncio.CancelledError()

    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    with pytest.raises(asyncio.CancelledError):
        await _run_locked(runner, sink, _cancelled_sink, preservation_sink)

    assert sink == []
    assert preservation_sink == ["AGENT_TIMEOUT"]


@pytest.mark.unit
async def test_an_untagged_cleanup_failure_is_never_marked_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No watchdog classification, no preservation claim — the caller still rolls back."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code=None)
    )
    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    result = await _run_locked(runner, sink, None, preservation_sink)

    assert result.returncode == 0
    assert sink == []
    assert preservation_sink == []


@pytest.mark.unit
async def test_a_published_floor_releases_the_protection_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor raise takes the protection over once the HEAD is published.

    Keeping the mark past that point would strand the *rerun's* own unaccepted
    residue on a later cancellation; the raised floor already keeps the timed-out
    run's commits out of that rollback.
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _dirty_sink(_reason_code: str) -> bool:
        return True

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    result = await _run_locked(runner, sink, _dirty_sink, preservation_sink)

    assert result.returncode == 0
    assert sink == [_PRE_RERUN_HEAD]
    assert preservation_sink == []


@pytest.mark.unit
async def test_an_unconfirmed_salvage_keeps_the_protection_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published floor is only a SHA, so it cannot take the mark over here.

    When the salvage could not confirm the timed-out run's edits are committed,
    the floor covers that run's *commits* and nothing else: releasing the mark
    would let the caller's cancellation branch reset to the floor over edits that
    are still dirty. Only a confirmed salvage hands the protection over
    (PRRT_kwDOSJAM6s6f4Ai7).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _dirty_sink(_reason_code: str) -> bool:
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink, _dirty_sink, preservation_sink)

    assert sink == [_PRE_RERUN_HEAD]
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]


@pytest.mark.unit
async def test_cancellation_after_an_unconfirmed_salvage_stays_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shielded salvage must not release the mark on its way out.

    A cancellation delivered once the shield lets it through reaches the caller's
    cancellation branch, which rolls back to the published floor unless the mark
    still stands — deleting the stranded edits an unconfirmed salvage left in the
    worktree (PRRT_kwDOSJAM6s6f4Ai7).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()

    async def _slow_unconfirmed_sink(_reason_code: str) -> bool:
        sink_started.set()
        await release_sink.wait()
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    item = asyncio.ensure_future(
        _run_locked(runner, sink, _slow_unconfirmed_sink, preservation_sink)
    )
    await sink_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_sink.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert sink == [_PRE_RERUN_HEAD]
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]
    assert runner.runs == 1


@pytest.mark.unit
async def test_an_unpublishable_floor_keeps_the_protection_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing published means nothing to hand over: the mark stays until the
    caller's own preserve handler takes the timeout."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=None)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink, None, preservation_sink)

    assert sink == []
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]


@pytest.mark.unit
async def test_cancellation_mid_sink_finishes_the_salvage_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marking the work protected is only the first thing this bookkeeping owes.

    A worker cancellation landing in the salvage sink used to escape with the
    claim already published: the caller's cancellation branch skipped the rollback
    (right) and its nested guard also skipped ``preserve_cancelled_timeout_work``,
    which only runs for timeouts no handler ever saw. The timed-out run's edits
    stayed dirty and the next pass rejected them as ``PRE_EXISTING_DIRTY_WORKTREE``
    (PRRT_kwDOSJAM6s6f3oxD).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()
    sink_finished: list[bool] = []

    async def _slow_sink(_reason_code: str) -> bool:
        sink_started.set()
        await release_sink.wait()
        sink_finished.append(True)
        return True

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    item = asyncio.ensure_future(_run_locked(runner, sink, _slow_sink, preservation_sink))
    await sink_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_sink.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert sink_finished == [True]
    # The floor is published too, so the caller's ``finally`` raises its rollback
    # floor over the salvage commit and the mark is handed over as usual.
    assert sink == [_PRE_RERUN_HEAD]
    assert preservation_sink == []
    assert runner.runs == 1


@pytest.mark.unit
async def test_cancellation_mid_sink_keeps_the_mark_when_no_floor_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished salvage with no floor to hand over stays protected."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=None)
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()
    sink_finished: list[bool] = []

    async def _slow_sink(_reason_code: str) -> bool:
        sink_started.set()
        await release_sink.wait()
        sink_finished.append(True)
        return True

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []
    preservation_sink: list[str] = []

    item = asyncio.ensure_future(_run_locked(runner, sink, _slow_sink, preservation_sink))
    await sink_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_sink.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert sink_finished == [True]
    assert sink == []
    assert preservation_sink == ["AGENT_IDLE_TIMEOUT"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("agent_reason_code", "expected_source_reason_code"),
    [
        ("AGENT_IDLE_TIMEOUT", "AGENT_IDLE_TIMEOUT"),
        ("AGENT_TIMEOUT", "AGENT_TIMEOUT"),
        (None, "EXEC_PROCESS_CLEANUP_FAILED"),
    ],
    ids=["idle_timeout", "timeout", "unmasked"],
)
async def test_the_rerun_guards_get_the_masked_timeout_not_the_cleanup_mask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_reason_code: str | None,
    expected_source_reason_code: str,
) -> None:
    """The retry guard classifies a masked timeout as the timeout, not the mask.

    ``exc.reason_code`` on the recovered cleanup failure is only
    EXEC_PROCESS_CLEANUP_FAILED, so a supersession abort inside the pre-launch
    guards would publish that as ``source_reason_code`` and drop the watchdog
    classification the rerun is recovering (PRRT_kwDOSJAM6s6f_MmO).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code=agent_reason_code)
    )
    guard_source_reason_codes: list[str] = []

    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        return 1

    async def _guards(*_args: object, source_reason_code: str, **_kwargs: object) -> None:
        guard_source_reason_codes.append(source_reason_code)

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_cleanup_error",
        _recover,
    )
    monkeypatch.setattr(
        agent_service_recovery,
        "_rerun_monitor_agent_pre_launch_guards",
        _guards,
    )

    async def _dirty_sink(_reason_code: str) -> bool:
        return True

    result = await _run_locked(runner, [], _dirty_sink)

    assert result.returncode == 0
    assert guard_source_reason_codes == [expected_source_reason_code]


class _StubSession:
    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.unit
async def test_a_supersession_abort_publishes_the_masked_timeout_as_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supersession abort on the rerun records the timeout, not the mask.

    This is the end of the carry the guard argument only starts: the abort
    persists ``source_reason_code`` into the operation's logs and events through
    ``_agent_service_recovery_source_details``, so the cleanup mask reaching the
    guard would lose the watchdog classification the rerun was recovering
    (PRRT_kwDOSJAM6s6f_MmO).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT"),
        session_factory=_StubSession,
    )

    async def _not_suppressed(_workspace_id: str) -> bool:
        return False

    runner._provider_recovery_suppresses_cli = _not_suppressed  # type: ignore[attr-defined]

    class _StubWorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get(self, _workspace_id: str) -> SimpleNamespace:
            return SimpleNamespace(status="completed", monitor_claimed_by=None)

    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        return 1

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_cleanup_error",
        _recover,
    )
    monkeypatch.setattr(agent_service_recovery, "WorkspaceRepository", _StubWorkspaceRepository)

    async def _dirty_sink(_reason_code: str) -> bool:
        return True

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError) as caught:
        await _run_locked(runner, [], _dirty_sink)

    assert caught.value.details["superseded_reason"] == "status_changed"
    assert caught.value.details["source_reason_code"] == "AGENT_TIMEOUT"


@pytest.mark.unit
async def test_callers_that_pass_no_preservation_sink_are_unaffected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monitor callers that do no floor bookkeeping keep their signature."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
    assert sink == [_PRE_RERUN_HEAD]
