"""The service-recovery loop publishes the HEAD it is about to rerun over (#934).

``_recover_monitor_agent_service_after_error`` only recovers watchdog timeouts,
so every rerun this loop performs replaces a run whose commits #932 forbids
deleting. The caller's rollback floor still points at the attempt start, so a
provider failure on the rerun would rewind past them. The loop therefore
publishes the pre-rerun HEAD through ``timeout_rerun_floor_sink`` so the caller
can raise that floor (PRRT_kwDOSJAM6s6fvdil).

A SHA floor cannot hold what that run left *uncommitted* — with no commits of its
own the published HEAD equals the attempt floor — so the loop first runs the
caller's ``timeout_rerun_dirty_sink``, the same dirty-worktree sink the #932
preserve handler uses, and publishes the HEAD it leaves behind
(PRRT_kwDOSJAM6s6fvw8r).

When no floor can be published at all the caller's stays at the attempt start, so
the rerun is given up and the timeout goes to the preserve handler instead
(PRRT_kwDOSJAM6s6fxp80).

That bookkeeping itself awaits while neither protection channel is populated, so
it claims the caller's ``timeout_preservation_sink`` for its duration: worker
cancellation there is a ``BaseException`` that bypasses every handler here and
lands on the caller's cancellation branch, which would rewind over the timed-out
run's work (PRRT_kwDOSJAM6s6fy7ju).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint_git_config as git_config,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

_PRE_RERUN_HEAD = "b" * 40
_TRUSTED_HEAD = "c" * 40
_SUNK_HEAD = "d" * 40
_WORKSPACE_ID = "ws_recovery"


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )


class _RecoveryRunner:
    """Minimal runner: the agent times out once, then the rerun succeeds."""

    def __init__(self, tmp_path: Path, *, head: str | None = _PRE_RERUN_HEAD) -> None:
        self._worktrees_root = tmp_path
        self._workspace_profile = None
        self._head = head
        self.head_reads: list[Path] = []
        self.runs = 0
        self._deps = SimpleNamespace(adapter=_TimeoutThenOkAdapter(self))

    async def _rev_parse_head(self, worktree_path: Path) -> str | None:
        self.head_reads.append(worktree_path)
        return self._head


class _TimeoutThenOkAdapter:
    is_hosted = False
    name = AgentRuntime.codex

    def __init__(self, runner: _RecoveryRunner) -> None:
        self._runner = runner

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        self._runner.runs += 1
        if self._runner.runs == 1:
            raise _timeout_error()
        return AgentRunResult(returncode=0, stdout="ok", stderr="")


def _stub_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recovered: int | None,
) -> None:
    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        return recovered

    async def _guards(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_error",
        _recover,
    )
    monkeypatch.setattr(
        agent_service_recovery,
        "_rerun_monitor_agent_pre_launch_guards",
        _guards,
    )


async def _run_locked(
    runner: _RecoveryRunner,
    sink: list[str] | None,
    dirty_sink: Any | None = None,
    preservation_sink: list[str] | None = None,
) -> AgentRunResult:
    return await agent_service_recovery._run_monitor_agent_with_service_recovery_locked(
        runner,
        workspace_id=_WORKSPACE_ID,
        compose_project="awf_ws_recovery",
        compose_file=runner._worktrees_root / "compose.yml",
        prompt="repair the review comment",
        log_source="recovery",
        timeout_rerun_floor_sink=sink,
        timeout_rerun_dirty_sink=dirty_sink,
        timeout_preservation_sink=preservation_sink,
    )


@pytest.mark.unit
async def test_recovered_timeout_publishes_the_pre_rerun_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor the caller must not rewind past is the timed-out run's HEAD."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
    assert runner.runs == 2
    assert sink == [_PRE_RERUN_HEAD]
    assert runner.head_reads == [tmp_path / _WORKSPACE_ID]


@pytest.mark.unit
async def test_an_unrecovered_timeout_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rerun means no lost run: the timeout reaches the #932 preserve handler."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    _stub_recovery(monkeypatch, recovered=None)
    sink: list[str] = []

    with pytest.raises(AgentRunError) as caught:
        await _run_locked(runner, sink)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert sink == []


@pytest.mark.unit
@pytest.mark.parametrize("head", [None, ""])
async def test_an_unreadable_head_gives_the_rerun_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head: str | None,
) -> None:
    """An unpublishable floor leaves the caller's at the attempt start.

    The caller raises its rollback floor only when the sink is non-empty, so a
    rerun with nothing published hands a provider failure or non-FIXED verdict on
    that rerun a ``reset --hard`` through the timed-out run's commits. Give the
    rerun up so the timeout reaches the #932 preserve handler instead
    (PRRT_kwDOSJAM6s6fxp80).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=head)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError) as caught:
        await _run_locked(runner, sink)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_a_missing_worktree_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to preserve when the worktree is gone, and nothing to probe."""
    runner = _RecoveryRunner(tmp_path)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    await _run_locked(runner, sink)

    assert sink == []
    assert runner.head_reads == []


@pytest.mark.unit
async def test_the_floor_probe_bounds_a_poisoned_live_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs after a watchdog timeout, so live Git may hang forever.

    An unbounded ``git rev-parse`` there would wedge the recovery loop: the rerun
    never starts and the timeout never reaches the #932 preserve handler
    (PRRT_kwDOSJAM6s6fvv27). The probe must pass a finite timeout.
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    timeouts: list[float | None] = []

    async def _head(
        worktree_path: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> str | None:
        runner.head_reads.append(worktree_path)
        timeouts.append(timeout_seconds)
        return _PRE_RERUN_HEAD

    runner._rev_parse_head = _head  # type: ignore[method-assign]
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
    assert sink == [_PRE_RERUN_HEAD]
    assert timeouts and timeouts[0] is not None and timeouts[0] > 0


@pytest.mark.unit
async def test_the_floor_probe_prefers_the_item_start_trusted_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A covered item-start snapshot must be probed instead of poisoned live config."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _trusted(_runner: object, _worktree_path: Path) -> str | None:
        return _TRUSTED_HEAD

    monkeypatch.setattr(git_config, "item_start_snapshot_covers_outer_git_dir", lambda _p: True)
    monkeypatch.setattr(git_config, "rev_parse_head_via_item_start_trust", _trusted)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
    assert sink == [_TRUSTED_HEAD]
    assert runner.head_reads == []


@pytest.mark.unit
@pytest.mark.parametrize("error", [OSError("git spawn failed"), ValueError("bad snapshot")])
async def test_a_head_probe_failure_gives_the_rerun_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A raising probe is swallowed, but it still publishes no floor.

    Swallowing keeps the watchdog reason code the preserve handler keys on; the
    missing floor still costs the rerun (PRRT_kwDOSJAM6s6fxp80).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _raise(_worktree_path: Path) -> str | None:
        raise error

    runner._rev_parse_head = _raise  # type: ignore[method-assign]
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError) as caught:
        await _run_locked(runner, sink)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_a_runner_without_a_head_probe_gives_the_rerun_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe seam, no floor — and so no rerun over the timed-out run's work."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    monkeypatch.delattr(_RecoveryRunner, "_rev_parse_head")
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink)

    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_the_timed_out_runs_dirty_edits_are_sunk_before_the_floor_is_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SHA floor cannot hold edits the timed-out run never committed.

    That run leaves them uncommitted and the published floor then equals the
    attempt floor, so a provider or protocol failure on the rerun resets straight
    through them (PRRT_kwDOSJAM6s6fvw8r). The dirty sink must therefore run
    *before* the HEAD probe, so the floor covers the commit it creates.
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
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
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink, _dirty_sink)

    assert result.returncode == 0
    assert calls == ["sink", "head"]
    assert sunk_reason_codes == ["AGENT_IDLE_TIMEOUT"]
    assert sink == [_SUNK_HEAD]


@pytest.mark.unit
async def test_the_dirty_sink_can_write_the_worktree_the_recovery_loop_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink commits, and the loop that calls it holds the writer lock.

    ``_commit_dirty_worktree`` — the sink the caller passes — stages and commits
    under ``hold_exclusive_worktree_writer_lock``, while the whole recovery loop
    already runs inside that same lock. A non-reentrant acquire would block the
    sink against its own holder and hang the monitor on exactly the timeout this
    salvage exists for, so drive the locking entry point end to end.
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    sunk: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        async with hold_exclusive_worktree_writer_lock(tmp_path / _WORKSPACE_ID):
            sunk.append(reason_code)
        runner._head = _SUNK_HEAD
        return True

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await agent_service_recovery._run_monitor_agent_with_service_recovery(
        runner,
        workspace_id=_WORKSPACE_ID,
        compose_project="awf_ws_recovery",
        compose_file=tmp_path / "compose.yml",
        prompt="repair the review comment",
        log_source="recovery",
        timeout_rerun_floor_sink=sink,
        timeout_rerun_dirty_sink=_dirty_sink,
    )

    assert result.returncode == 0
    assert sunk == ["AGENT_IDLE_TIMEOUT"]
    assert sink == [_SUNK_HEAD]


@pytest.mark.unit
async def test_a_failing_dirty_sink_never_costs_the_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Salvage bookkeeping is not allowed to abort the recovery it serves."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _dirty_sink(_reason_code: str) -> bool:
        raise RuntimeError("commit sink exploded")

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink, _dirty_sink)

    assert result.returncode == 0
    assert runner.runs == 2
    assert sink == [_PRE_RERUN_HEAD]


@pytest.mark.unit
async def test_an_unconfirmed_salvage_gives_the_rerun_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stranded edits outrank the rerun: the timeout goes to the preserve handler.

    A published floor is only a SHA, so it cannot cover edits the salvage failed
    to commit. Rerunning over them hands a provider failure or non-FIXED verdict
    on the rerun a ``reset --hard`` straight through work #932 promised to keep,
    while giving the rerun up leaves those edits exactly where the timed-out run
    left them (PRRT_kwDOSJAM6s6fwTyO).
    """
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    sunk_reason_codes: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sunk_reason_codes.append(reason_code)
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError) as caught:
        await _run_locked(runner, sink, _dirty_sink)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.runs == 1
    assert sunk_reason_codes == ["AGENT_IDLE_TIMEOUT"]
    # The floor is still published: the timed-out run's *commits* must survive
    # whatever rollback the caller's exit performs.
    assert sink == [_PRE_RERUN_HEAD]


@pytest.mark.unit
async def test_an_unconfirmed_salvage_gives_a_cleanup_failure_rerun_up_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup-recovery branch reruns the same way, so it aborts the same way."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    sunk_reason_codes: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sunk_reason_codes.append(reason_code)
        return False

    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(ComposeExecCleanupError):
        await _run_locked(runner, sink, _dirty_sink)

    assert runner.runs == 1
    assert sunk_reason_codes == ["AGENT_TIMEOUT"]
    assert sink == [_PRE_RERUN_HEAD]


@pytest.mark.unit
async def test_an_unconfirmed_salvage_without_a_readable_head_still_stops_the_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor probe's own outcome cannot license a rerun over stranded edits."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=None)

    async def _dirty_sink(_reason_code: str) -> bool:
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink, _dirty_sink)

    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_an_unconfirmed_salvage_stops_the_rerun_without_a_head_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe seam means no floor — and still no rerun over stranded edits."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    monkeypatch.delattr(_RecoveryRunner, "_rev_parse_head")

    async def _dirty_sink(_reason_code: str) -> bool:
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink, _dirty_sink)

    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_an_unconfirmed_salvage_stops_the_rerun_when_the_head_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed floor probe publishes nothing, but the salvage verdict still stands."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _raise(_worktree_path: Path) -> str | None:
        raise OSError("git spawn failed")

    async def _dirty_sink(_reason_code: str) -> bool:
        return False

    runner._rev_parse_head = _raise  # type: ignore[method-assign]
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(AgentRunError):
        await _run_locked(runner, sink, _dirty_sink)

    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_an_unpublishable_floor_gives_a_cleanup_failure_rerun_up_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup-recovery branch reruns the same way, so it aborts the same way."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=None)
    runner._deps = SimpleNamespace(
        adapter=_CleanupErrorThenOkAdapter(runner, agent_reason_code="AGENT_TIMEOUT")
    )
    _stub_cleanup_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    with pytest.raises(ComposeExecCleanupError):
        await _run_locked(runner, sink)

    assert runner.runs == 1
    assert sink == []


@pytest.mark.unit
async def test_a_missing_worktree_sinks_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No worktree, no edits to salvage — and no sink call to make."""
    runner = _RecoveryRunner(tmp_path)
    sink_calls: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sink_calls.append(reason_code)
        return False

    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    await _run_locked(runner, sink, _dirty_sink)

    assert sink_calls == []
    assert sink == []


@pytest.mark.unit
async def test_an_unrecovered_timeout_sinks_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a rerun the timeout reaches the #932 handler, which sinks it there."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    sink_calls: list[str] = []

    async def _dirty_sink(reason_code: str) -> bool:
        sink_calls.append(reason_code)
        return False

    _stub_recovery(monkeypatch, recovered=None)

    with pytest.raises(AgentRunError):
        await _run_locked(runner, [], _dirty_sink)

    assert sink_calls == []


def _cleanup_error(*, agent_reason_code: str | None) -> ComposeExecCleanupError:
    exc = ComposeExecCleanupError(
        invocation_id="inv-1",
        source="recovery",
        label="agent",
        message='service "agent" is not running',
    )
    exc.agent_reason_code = agent_reason_code
    return exc


class _CleanupErrorThenOkAdapter:
    is_hosted = False
    name = AgentRuntime.codex

    def __init__(self, runner: _RecoveryRunner, *, agent_reason_code: str | None) -> None:
        self._runner = runner
        self._agent_reason_code = agent_reason_code

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        self._runner.runs += 1
        if self._runner.runs == 1:
            raise _cleanup_error(agent_reason_code=self._agent_reason_code)
        return AgentRunResult(returncode=0, stdout="ok", stderr="")


def _stub_cleanup_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recovered: int | None,
) -> None:
    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        return recovered

    async def _guards(*_args: object, **_kwargs: object) -> None:
        return None

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
