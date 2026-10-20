"""An item never fails the workspace once its rollback succeeded — #1074 Scope C (part 030).

When the correction attempt still ends without an acceptable verdict, the
``protocol_attempt == 1`` arm used to roll the item back and *raise*.
``fix_cycle`` treats that as terminal: it skips the end-of-batch push and fails
the workspace, stranding every accepted commit in the batch. The arm now returns
an item outcome that reuses the existing escalation shapes:

* a contentful commit in the item's range is kept and the item parks
  ``needs_human``, citing the commit and the reason code (the
  ``correction_unscoped_fix_outcome`` shape);
* otherwise the residue is rolled back as before and the item parks
  ``needs_human`` with the reason code (the ``resolve_correction_non_fixed_mutation``
  shape, #1020).

A rollback that itself fails still raises the typed terminal error, because the
worktree state is then unknown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner
from tests.unit.runtime._verdict_trailing_output_fixtures import F3, F4, F4_REASON

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_FIX_HEAD = "b" * 40
_REVIEWED_PATH = "src/app.py"
_PRESERVED_EVENT = "monitor.agent_verdict_correction_unaccepted_commit_preserved"
_PARKED_EVENT = "monitor.agent_verdict_correction_unaccepted_item_parked"
_LEFTOVER_CLEANED_EVENT = "monitor.agent_verdict_correction_unaccepted_leftover_edits_cleaned"


async def _invoke_item(runner: _VerdictRunner) -> VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_REVIEWED_PATH,
        evidence_item_line=10,
    )


def _runner(
    tmp_path: Path,
    *,
    outputs: list[str],
    heads: list[str],
    dirty: list[bool] | None = None,
    stranded: list[bool] | None = None,
    reset_fails: bool = False,
) -> _VerdictRunner:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=list(outputs),
        heads_after_attempt=heads,
        dirty_after_attempt=dirty,
        stranded_dirty_after_attempt=stranded,
        reset_fails=reset_fails,
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


def _committed_runner(tmp_path: Path, outputs: list[str]) -> _VerdictRunner:
    """Attempt 0 commits a contentful change; nothing else moves HEAD."""
    return _runner(tmp_path, outputs=outputs, heads=[_FIX_HEAD, _FIX_HEAD], dirty=[True, False])


def _empty_commit_runner(tmp_path: Path, *, reset_fails: bool = False) -> _VerdictRunner:
    """Attempt 0 advances HEAD with a commit whose tree equals the item start."""
    runner = _runner(
        tmp_path,
        outputs=[F3, F3],
        heads=[_FIX_HEAD, _FIX_HEAD],
        dirty=[True, False],
        reset_fails=reset_fails,
    )

    async def _same_tree(**_kwargs: object) -> bool:
        return False

    runner._commit_trees_differ = _same_tree
    return runner


def _events(captured: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in captured if entry.get("event") == event]


@pytest.mark.unit
async def test_record_less_item_with_a_contentful_commit_keeps_it_and_parks(
    tmp_path: Path,
) -> None:
    """Scenario 5: F3 on both attempts with a contentful commit → keep, park, no raise."""
    runner = _committed_runner(tmp_path, [F3, F3])

    with structlog.testing.capture_logs() as captured:
        result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_VERDICT_PROTOCOL_VIOLATION}:")
    assert _FIX_HEAD[:12] in result.reason
    assert "preserved for human review" in result.reason
    assert result.kept_commit is True
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    preserved = _events(captured, _PRESERVED_EVENT)
    assert len(preserved) == 1
    assert preserved[0]["reason_code"] == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert preserved[0]["attempt_tip"] == _FIX_HEAD
    assert _events(captured, _PARKED_EVENT) == []


def _stranded_commit_runner(tmp_path: Path) -> _VerdictRunner:
    """Attempt 0 self-commits a contentful change; the correction's sink strands later edits."""
    return _runner(
        tmp_path,
        outputs=[F3, F3],
        heads=[_FIX_HEAD, _FIX_HEAD],
        stranded=[False, True],
    )


def _spy_leftover_clean(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clean_ok: bool | None = None,
) -> list[dict[str, object]]:
    """Record rollback calls; ``clean_ok`` replaces the result, ``None`` delegates."""
    calls: list[dict[str, object]] = []
    real_rollback = comment_verdict._rollback_or_classify_failure

    async def _rollback(runner: object, **kwargs: object) -> bool:
        calls.append(kwargs)
        if clean_ok is not None:
            return clean_ok
        return await real_rollback(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_rollback_or_classify_failure", _rollback)
    return calls


@pytest.mark.unit
async def test_leftover_edits_above_a_kept_commit_are_cleaned_before_parking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6uOQn5: the next item must not absorb this item's unfinished edits.

    The sink failed after the item's contentful commit, so edits are still dirty.
    They are cleaned back to the kept commit (HEAD unmoved, no hosted or push
    tracking rewind), and the commit itself is kept.
    """
    clean_calls = _spy_leftover_clean(monkeypatch)
    runner = _stranded_commit_runner(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert result.reason is not None
    assert _FIX_HEAD[:12] in result.reason
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    assert len(clean_calls) == 1
    assert clean_calls[0]["item_start_head"] == _FIX_HEAD
    assert clean_calls[0]["state"] is None
    cleaned = _events(captured, _LEFTOVER_CLEANED_EVENT)
    assert len(cleaned) == 1
    assert cleaned[0]["attempt_tip"] == _FIX_HEAD
    assert len(_events(captured, _PRESERVED_EVENT)) == 1


@pytest.mark.unit
async def test_a_clean_worktree_above_a_kept_commit_needs_no_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_calls = _spy_leftover_clean(monkeypatch)
    runner = _committed_runner(tmp_path, [F3, F3])

    result = await _invoke_item(runner)

    assert result.kept_commit is True
    assert clean_calls == []


def _spy_git_config_restore(
    monkeypatch: pytest.MonkeyPatch,
    *,
    restore_ok: bool,
) -> list[Path]:
    """Record item-start Git config restores; each returns ``restore_ok``."""
    calls: list[Path] = []

    def _restore(worktree_path: Path) -> bool:
        calls.append(worktree_path)
        return restore_ok

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint."
        "restore_item_start_local_git_configs",
        _restore,
    )
    return calls


@pytest.mark.unit
async def test_config_only_residue_above_a_kept_commit_restores_the_item_start_git_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6uO5Xk: a ``git-meta:``-only fingerprint still restores Git config.

    Config-only residue carries no path residue, so no clean runs, but settings
    such as ``include.path`` or ``url.*.insteadOf`` must not reach the next item
    or the push. HEAD and the kept commit stay put.
    """
    clean_calls = _spy_leftover_clean(monkeypatch)
    restore_calls = _spy_git_config_restore(monkeypatch, restore_ok=True)
    runner = _committed_runner(tmp_path, [F3, F3])

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert clean_calls == []
    assert restore_calls == [tmp_path / "ws_protocol"]
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
async def test_a_failed_git_config_restore_above_a_kept_commit_is_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrestorable Git config leaves the worktree state unknown, so it stays terminal."""
    clean_calls = _spy_leftover_clean(monkeypatch)
    restore_calls = _spy_git_config_restore(monkeypatch, restore_ok=False)
    runner = _committed_runner(tmp_path, [F3, F3])

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert clean_calls == []
    assert len(restore_calls) == 1
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    failed = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_leftover_edits_clean_failed"
    )
    assert len(failed) == 1
    assert failed[0]["attempt_tip"] == _FIX_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("live_head", ["moved", "unreadable"])
async def test_a_git_config_restore_that_loses_the_kept_commit_at_head_is_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_head: str,
) -> None:
    """PRRT_kwDOSJAM6s6uPDnk: HEAD is re-read after the config-only restore.

    The restore also rewrites the gitfile, ``commondir`` and nested linkage, so
    HEAD can then resolve in a git-dir that does not hold the kept commit.
    Parking with ``kept_commit`` would let the push publish that repository
    instead of the kept tip, so a moved or unverifiable HEAD stays terminal.
    """
    clean_calls = _spy_leftover_clean(monkeypatch)
    runner = _committed_runner(tmp_path, [F3, F3])
    restore_calls: list[Path] = []
    moved_head = "c" * 40

    def _restore(worktree_path: Path) -> bool:
        restore_calls.append(worktree_path)
        if live_head == "moved":
            runner.current_head = moved_head
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint."
        "restore_item_start_local_git_configs",
        _restore,
    )
    if live_head == "unreadable":

        async def _unreadable_tip(_runner: object, **kwargs: object) -> str | None:
            return kwargs["fallback"]  # type: ignore[return-value]

        monkeypatch.setattr(comment_verdict, "preserved_correction_tip", _unreadable_tip)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert clean_calls == []
    assert restore_calls == [tmp_path / "ws_protocol"]
    assert runner.reset_targets == []
    moved = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_git_config_restore_moved_head"
    )
    if live_head == "moved":
        assert len(moved) == 1
        assert moved[0]["attempt_tip"] == _FIX_HEAD
        assert moved[0]["live_head"] == moved_head
    else:
        assert moved == []
    failed = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_leftover_edits_clean_failed"
    )
    assert len(failed) == 1


@pytest.mark.unit
async def test_config_only_residue_without_an_item_start_git_config_baseline_is_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6uPByi: a restore with no saved baseline proves nothing.

    The item-start snapshot may fail without aborting the item, which leaves no
    cached baseline. The restore then reports success without rewriting config,
    so an agent-added ``include.path`` could reach the next item or the push.
    """

    def _snapshot_fails(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot_fails)
    clean_calls = _spy_leftover_clean(monkeypatch)
    runner = _committed_runner(tmp_path, [F3, F3])

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert clean_calls == []
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    failed = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_leftover_edits_clean_failed"
    )
    assert len(failed) == 1
    assert failed[0]["attempt_tip"] == _FIX_HEAD


@pytest.mark.unit
async def test_leftover_edits_without_an_item_start_git_config_baseline_are_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6uPByi: the path-residue clean needs the baseline too.

    The clean restores item-start Git config before resetting paths, and that
    restore also reports success with no saved baseline. Cleaning the paths
    would then let an agent-added ``include.path`` reach the next item or the
    push, so it fails closed before the clean.
    """

    def _snapshot_fails(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(comment_verdict, "remember_item_start_local_git_configs", _snapshot_fails)
    clean_calls = _spy_leftover_clean(monkeypatch)
    runner = _stranded_commit_runner(tmp_path)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert clean_calls == []
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    assert _events(captured, _LEFTOVER_CLEANED_EVENT) == []
    failed = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_leftover_edits_clean_failed"
    )
    assert len(failed) == 1
    assert failed[0]["attempt_tip"] == _FIX_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["clean_fails", "tip_unreadable"])
async def test_leftover_edits_that_cannot_be_cleaned_are_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Unknown worktree state stays terminal, and the kept commit is never reset away."""
    clean_calls = _spy_leftover_clean(monkeypatch, clean_ok=False)
    if failure == "tip_unreadable":

        async def _unreadable_tip(_runner: object, **kwargs: object) -> str | None:
            return kwargs["fallback"]  # type: ignore[return-value]

        monkeypatch.setattr(comment_verdict, "preserved_correction_tip", _unreadable_tip)
    runner = _stranded_commit_runner(tmp_path)

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    assert len(clean_calls) == (1 if failure == "clean_fails" else 0)


@pytest.mark.unit
@pytest.mark.parametrize("probe", ["unreadable", "raises"])
async def test_an_unreadable_residue_probe_above_a_kept_commit_fails_closed_into_the_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
) -> None:
    """PRRT_kwDOSJAM6s6uOogH: a raising probe is unreadable, not an untyped failure.

    The probe spawns Git and can raise outside its own fail-closed handlers. Once
    a commit is kept, that must still clean back to the kept commit and park.
    """
    clean_calls = _spy_leftover_clean(monkeypatch)
    real_tip = comment_verdict.preserved_correction_tip
    real_probe = comment_verdict._read_correction_pr_worthy_residue_fingerprint
    parking = False

    async def _tip(runner: object, **kwargs: object) -> str | None:
        nonlocal parking
        parking = True
        return await real_tip(runner, **kwargs)  # type: ignore[arg-type]

    async def _probe(runner: object, **kwargs: object) -> str | None:
        if not parking:
            return await real_probe(runner, **kwargs)  # type: ignore[arg-type]
        if probe == "raises":
            raise RuntimeError("git status timed out")
        return None

    monkeypatch.setattr(comment_verdict, "preserved_correction_tip", _tip)
    monkeypatch.setattr(comment_verdict, "_read_correction_pr_worthy_residue_fingerprint", _probe)
    runner = _committed_runner(tmp_path, [F3, F3])

    with structlog.testing.capture_logs() as captured:
        result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
    assert len(clean_calls) == 1
    assert clean_calls[0]["item_start_head"] == _FIX_HEAD
    assert clean_calls[0]["state"] is None
    assert len(_events(captured, _LEFTOVER_CLEANED_EVENT)) == 1
    probe_failures = _events(
        captured, "monitor.agent_verdict_correction_unaccepted_leftover_residue_probe_failed"
    )
    assert [entry.get("exc_type") for entry in probe_failures] == (
        ["RuntimeError"] if probe == "raises" else []
    )


@pytest.mark.unit
async def test_record_less_item_without_any_change_parks(tmp_path: Path) -> None:
    """Scenario 6: F3 on both attempts, nothing committed → park, no raise."""
    runner = _runner(tmp_path, outputs=[F3, F3], heads=[_ITEM_START_HEAD, _ITEM_START_HEAD])

    with structlog.testing.capture_logs() as captured:
        result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_VERDICT_PROTOCOL_VIOLATION}:")
    assert f"rolled back to {_ITEM_START_HEAD[:12]}" in result.reason
    assert runner.current_head == _ITEM_START_HEAD
    parked = _events(captured, _PARKED_EVENT)
    assert len(parked) == 1
    assert parked[0]["reason_code"] == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert _events(captured, _PRESERVED_EVENT) == []


@pytest.mark.unit
async def test_a_commit_with_no_content_is_rolled_back_before_parking(tmp_path: Path) -> None:
    """Scenario 6: an empty commit is residue, not work. It goes, and the item parks."""
    runner = _empty_commit_runner(tmp_path)

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason
    assert result.kept_commit is False
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD


@pytest.mark.unit
async def test_a_failed_rollback_is_still_terminal(tmp_path: Path) -> None:
    """Scenario 10: unknown worktree state stays a typed terminal error, unchanged."""
    runner = _empty_commit_runner(tmp_path, reset_fails=True)

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_false_positive_with_chatter_is_corrected_normally(tmp_path: Path) -> None:
    """Scenario 7: F4 is never accepted leniently, but a clean correction is."""
    runner = _runner(
        tmp_path,
        outputs=[F4, f"AWF-VERDICT: FALSE POSITIVE: {F4_REASON}"],
        heads=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    result = await _invoke_item(runner)

    assert result == VerdictResult(verdict="false_positive", reason=F4_REASON)
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_false_positive_with_chatter_twice_parks_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Scenario 7: the correction repeats F4, so the item parks and the workspace lives."""
    runner = _runner(tmp_path, outputs=[F4, F4], heads=[_ITEM_START_HEAD, _ITEM_START_HEAD])

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_hint_fixed_then_rerun_still_fails_without_a_commit_parks(tmp_path: Path) -> None:
    """Scenario 3: strict contract for a commit-less hint FIXED, and no workspace failure."""
    stdout = "AWF-VERDICT: FIXED: re-ran the failing CI job\nthe rerun still fails"
    runner = _runner(
        tmp_path,
        outputs=[stdout, stdout],
        heads=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    result = await _invoke(runner, require_fix_evidence=False)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_VERDICT_PROTOCOL_VIOLATION}:")
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_strict_fixed_twice_without_any_change_parks(tmp_path: Path) -> None:
    """``AGENT_FIXED_WITHOUT_EVIDENCE`` after the correction parks with its own code."""
    runner = _runner(
        tmp_path,
        outputs=["AWF-VERDICT: FIXED: claimed once", "AWF-VERDICT: FIXED: claimed twice"],
        heads=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_FIXED_WITHOUT_EVIDENCE}:")


def _fail_unscoped_evidence_probe(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """Make only the anchor-free (``item_path=None``) evidence probe raise."""
    real_evidence = comment_verdict._item_fix_evidence

    async def _probe(runner: object, **kwargs: object) -> bool:
        if kwargs.get("item_path") is None:
            raise error
        return await real_evidence(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)


@pytest.mark.unit
async def test_park_probe_failure_rolls_back_then_reraises_the_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keep-or-rollback probe runs Git outside the sink handlers; never strand edits."""
    probe_error = OSError("git rev-parse spawn failed")
    _fail_unscoped_evidence_probe(monkeypatch, probe_error)
    runner = _committed_runner(tmp_path, [F3, F3])

    with pytest.raises(OSError) as caught:
        await _invoke_item(runner)

    assert caught.value is probe_error
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD


@pytest.mark.unit
async def test_park_probe_failure_with_a_failed_rollback_is_typed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_error = OSError("git rev-parse spawn failed")
    _fail_unscoped_evidence_probe(monkeypatch, probe_error)
    runner = _runner(
        tmp_path,
        outputs=[F3, F3],
        heads=[_FIX_HEAD, _FIX_HEAD],
        dirty=[True, False],
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "terminating after protocol violation" in str(caught.value)
    assert caught.value.__cause__ is probe_error


@pytest.mark.unit
async def test_an_unknown_rollback_floor_stays_terminal(tmp_path: Path) -> None:
    """Nothing can be measured or rolled back, so the item must not park as rolled back.

    With no readable start HEAD at all, a park would claim a rollback that never
    happened and let the batch push publish the unaccepted commit.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[F3, F3],
        heads_after_attempt=[_FIX_HEAD, _FIX_HEAD],
        rev_parse_sequence=[None, None],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
        )

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_a_missing_worktree_stays_terminal(tmp_path: Path) -> None:
    """No local worktree means no rollback actually ran, so there is nothing to park on."""
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[F3, F3],
        heads_after_attempt=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke_item(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_the_park_probe_resolves_through_comment_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-exported guarded probe is the seam a monkeypatch on ``comment_verdict`` reaches."""
    calls: list[str | None] = []

    async def _no_contentful_commit(_runner: object, **kwargs: object) -> bool:
        calls.append(str(kwargs["rollback_failure_message"]))
        return False

    monkeypatch.setattr(comment_verdict, "guarded_unscoped_fix_evidence", _no_contentful_commit)
    runner = _committed_runner(tmp_path, [F3, F3])

    result = await _invoke_item(runner)

    assert len(calls) == 1
    assert "terminating after protocol violation" in (calls[0] or "")
    assert result.verdict == "needs_human"
    assert runner.reset_targets == [_ITEM_START_HEAD]


async def _address(runner: _VerdictRunner, state: MonitorState) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=ReviewThread(
            thread_id="PRRT_record_less",
            path=_REVIEWED_PATH,
            line=10,
            body_excerpt="please fix",
            author="reviewer",
        ),
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
@pytest.mark.parametrize("committed", [True, False])
async def test_address_thread_records_the_park_on_the_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    """The bridge to the batch: ``needs_human`` with the reason code on the thread."""

    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)
    if committed:
        runner = _committed_runner(tmp_path, [F3, F3])
    else:
        runner = _runner(tmp_path, outputs=[F3, F3], heads=[_ITEM_START_HEAD, _ITEM_START_HEAD])
    state = MonitorState()

    verdict = await _address(runner, state)

    assert verdict == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key("PRRT_record_less")]
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in parked_reason
    assert runner.current_head == (_FIX_HEAD if committed else _ITEM_START_HEAD)
