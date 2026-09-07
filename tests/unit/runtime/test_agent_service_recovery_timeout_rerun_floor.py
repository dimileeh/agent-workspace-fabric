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
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint_git_config as git_config,
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
async def test_an_unreadable_head_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head: str | None,
) -> None:
    """A HEAD that cannot be read leaves the caller's floor exactly where it was."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path, head=head)
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    await _run_locked(runner, sink)

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
async def test_a_head_probe_failure_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A raising probe is logged and skipped — never allowed to mask the rerun."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)

    async def _raise(_worktree_path: Path) -> str | None:
        raise error

    runner._rev_parse_head = _raise  # type: ignore[method-assign]
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
    assert sink == []


@pytest.mark.unit
async def test_a_runner_without_a_head_probe_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe seam, no floor — the rerun still happens."""
    (tmp_path / _WORKSPACE_ID).mkdir()
    runner = _RecoveryRunner(tmp_path)
    monkeypatch.delattr(_RecoveryRunner, "_rev_parse_head")
    _stub_recovery(monkeypatch, recovered=1)
    sink: list[str] = []

    result = await _run_locked(runner, sink)

    assert result.returncode == 0
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
