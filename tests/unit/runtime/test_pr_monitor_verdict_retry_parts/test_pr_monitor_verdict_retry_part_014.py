"""A re-armed #932 anchor is dropped once a rebase strands it (#934 audit).

The preserved item-start marker is now re-armed for every attempt that dies
before a verdict, so it can outlive several failed passes — long enough for a
``SyncBase`` rebase to rewrite the branch and leave the anchor on a dropped SHA.
An attempt that anchors there gets an evidence range git cannot resolve, so its
honest ``FIXED`` can never be proven and the item wedges.

The anchor is therefore checked against this attempt's start HEAD before use:
a definitive "not an ancestor" drops it (and it is not re-armed afterwards),
while an unreadable probe keeps it, since dropping also costs the preserved
commits their place in the item's own evidence range.
"""

from __future__ import annotations

import errno
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner import comment_verdict_timeout_preserve as timeout_preserve
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    preserved_anchor_is_reachable,
    remember_item_start_head,
)
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_REATTEMPT_HEAD = "c" * 40
_ITEM_ID = "issue:5558086911"


class _AnchorProbeRunner(_VerdictRunner):
    """Verdict runner that controls the ``merge-base --is-ancestor`` anchor probe."""

    def __init__(
        self,
        *,
        anchor_is_ancestor: bool = True,
        anchor_probe_raises: bool = False,
        anchor_probe_returncode: int | None = None,
        anchor_object_exists: bool = True,
        anchor_existence_probe_raises: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.anchor_is_ancestor = anchor_is_ancestor
        self.anchor_probe_raises = anchor_probe_raises
        self.anchor_probe_returncode = anchor_probe_returncode
        self.anchor_object_exists = anchor_object_exists
        self.anchor_existence_probe_raises = anchor_existence_probe_raises
        self.anchor_probes: list[tuple[str, str]] = []
        self.anchor_existence_probes: list[str] = []
        self.evidence_ancestors: list[str] = []

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "merge-base" in cmd and "--is-ancestor" in cmd:
            self.anchor_probes.append((cmd[-2], cmd[-1]))
            if self.anchor_probe_raises:
                raise OSError("git merge-base spawn failed")
            if self.anchor_probe_returncode is not None:
                return CommandResult(
                    returncode=self.anchor_probe_returncode,
                    stdout="",
                    stderr="fatal: probe did not answer",
                )
            return CommandResult(
                returncode=0 if self.anchor_is_ancestor else 1,
                stdout="",
                stderr="",
            )
        if "rev-parse" in cmd and "--verify" in cmd:
            self.anchor_existence_probes.append(cmd[-1])
            if self.anchor_existence_probe_raises:
                raise OSError("git rev-parse spawn failed")
            if self.anchor_object_exists:
                return CommandResult(returncode=0, stdout=f"{_ITEM_START_HEAD}\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="")
        return await super()._run_git(cmd, **kwargs)

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        self.evidence_ancestors.append(ancestor)
        return await super()._head_descends_from(
            worktree_path=worktree_path,
            ancestor=ancestor,
            descendant=descendant,
        )


def _state_after_preserved_timeout() -> MonitorState:
    """The state #932 leaves behind: the original item start remembered."""
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _ITEM_START_HEAD)
    return state


async def _invoke_reattempt(
    runner: _AnchorProbeRunner,
    *,
    state: MonitorState,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        # What the next monitor pass passes in: the live (preserved) HEAD.
        operation_start_head=_PRESERVED_HEAD,
        evidence_item_id=_ITEM_ID,
    )


@pytest.mark.unit
async def test_a_rebase_stranded_anchor_is_not_used_for_evidence(tmp_path: Path) -> None:
    """The stale anchor is dropped, so FIXED is measured from this attempt's start."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: re-applied the fix after the rebase"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.anchor_probes == [(_ITEM_START_HEAD, _PRESERVED_HEAD)]
    assert runner.evidence_ancestors == [_PRESERVED_HEAD]


@pytest.mark.unit
async def test_a_reachable_anchor_is_still_used_for_evidence(tmp_path: Path) -> None:
    """The #932 concession stands while the anchor is still in the branch history."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.anchor_probes == [(_ITEM_START_HEAD, _PRESERVED_HEAD)]
    assert runner.evidence_ancestors == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_a_stranded_anchor_is_not_re_armed_by_a_failed_attempt(tmp_path: Path) -> None:
    """Dropping is permanent: the failure path must not resurrect a dead anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_reattempt(runner, state=state)

    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_an_unreadable_anchor_probe_keeps_the_anchor(tmp_path: Path) -> None:
    """Probe uncertainty must not cost the preserved commits their evidence range."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_probe_raises=True,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.evidence_ancestors == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_an_anchor_equal_to_the_attempt_start_skips_the_probe(tmp_path: Path) -> None:
    """No rebase can strand an anchor that is already this attempt's start HEAD."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _PRESERVED_HEAD)

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.anchor_probes == []
    assert runner.evidence_ancestors == [_PRESERVED_HEAD]


@pytest.mark.unit
@pytest.mark.parametrize("probe_returncode", [124, 128])
async def test_a_non_answering_probe_exit_keeps_a_still_present_anchor(
    tmp_path: Path,
    probe_returncode: int,
) -> None:
    """A timed-out (124) or fatal (128) probe is not a "not an ancestor" answer."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_probe_returncode=probe_returncode,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )

    reachable = await preserved_anchor_is_reachable(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_protocol",
        anchor_head=_ITEM_START_HEAD,
        attempt_start_head=_PRESERVED_HEAD,
    )

    assert reachable is True
    assert runner.anchor_existence_probes == [f"{_ITEM_START_HEAD}^{{commit}}"]


@pytest.mark.unit
async def test_a_non_answering_probe_drops_an_anchor_git_no_longer_has(
    tmp_path: Path,
) -> None:
    """A fatal probe over a pruned anchor is still the stranding this guard exists for."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_probe_returncode=128,
        anchor_object_exists=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )

    reachable = await preserved_anchor_is_reachable(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_protocol",
        anchor_head=_ITEM_START_HEAD,
        attempt_start_head=_PRESERVED_HEAD,
    )

    assert reachable is False


@pytest.mark.unit
async def test_an_unreadable_existence_probe_keeps_the_anchor(tmp_path: Path) -> None:
    """Two non-answers in a row still prove nothing about the anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_probe_returncode=124,
        anchor_existence_probe_raises=True,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )

    reachable = await preserved_anchor_is_reachable(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_protocol",
        anchor_head=_ITEM_START_HEAD,
        attempt_start_head=_PRESERVED_HEAD,
    )

    assert reachable is True


def _misbehaving_exists(worktree_path: Path, failure: Callable[[], object]) -> Callable[..., bool]:
    """``Path.exists`` that misbehaves for ``worktree_path`` and is honest elsewhere."""
    original_exists = Path.exists

    def _exists(self: Path, **kwargs: Any) -> bool:
        if self == worktree_path:
            failure()
        return bool(original_exists(self, **kwargs))

    return _exists


@pytest.mark.unit
async def test_an_unreadable_presence_probe_keeps_the_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising presence check must not escape the anchor guard.

    ``Path.exists()`` only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so a transient
    EIO/EACCES on the worktree a timed-out agent was last touching would leave
    this guard as an unrelated exception instead of a verdict
    (PRRT_kwDOSJAM6s6f5q9B). Unknown is not "not an ancestor": keep the anchor.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )

    def _unreadable() -> object:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "exists", _misbehaving_exists(tmp_path / "ws_protocol", _unreadable))

    reachable = await preserved_anchor_is_reachable(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_protocol",
        anchor_head=_ITEM_START_HEAD,
        attempt_start_head=_PRESERVED_HEAD,
    )

    assert reachable is True
    assert runner.anchor_probes == []


@pytest.mark.unit
async def test_a_stalled_presence_probe_does_not_wedge_the_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``stat`` against a wedged mount must not block the monitor's event loop.

    This check runs ahead of both bounded Git probes, so an unbounded synchronous
    ``stat`` freezes the whole monitor worker — unrelated workspaces included —
    for as long as the mount stays wedged (PRRT_kwDOSJAM6s6f5q9B).
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    release = threading.Event()

    def _stall() -> object:
        release.wait(timeout=30)
        return None

    monkeypatch.setattr(Path, "exists", _misbehaving_exists(tmp_path / "ws_protocol", _stall))
    monkeypatch.setattr(timeout_preserve, "_WORKTREE_PRESENCE_PROBE_TIMEOUT_SECONDS", 0.05)

    try:
        reachable = await preserved_anchor_is_reachable(
            runner,  # type: ignore[arg-type]
            worktree_path=tmp_path / "ws_protocol",
            anchor_head=_ITEM_START_HEAD,
            attempt_start_head=_PRESERVED_HEAD,
        )
    finally:
        release.set()

    assert reachable is True
    assert runner.anchor_probes == []


@pytest.mark.unit
async def test_a_missing_worktree_keeps_the_anchor(tmp_path: Path) -> None:
    """An absent worktree cannot answer the probe, so the anchor survives."""
    runner = _AnchorProbeRunner(
        anchor_is_ancestor=False,
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: finished the preserved work"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )

    reachable = await preserved_anchor_is_reachable(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_gone",
        anchor_head=_ITEM_START_HEAD,
        attempt_start_head=_PRESERVED_HEAD,
    )

    assert reachable is True
    assert runner.anchor_probes == []
