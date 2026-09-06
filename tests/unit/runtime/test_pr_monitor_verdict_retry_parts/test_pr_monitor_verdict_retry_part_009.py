"""Correction-attempt evidence hardening (follow-up to #925 / PR #926).

ws_46bc0f45 (PR #922 monitor) died with ``AGENT_FIXED_WITHOUT_EVIDENCE`` on the
correction attempt: attempt 0 was a protocol violation (the agent left out the
verdict line while a background test sweep ran), attempt 1 a legitimate FIXED
whose hunks sat ~100 lines from the anchor in the anchored file. PR #926 only
escalated evidence after an *evidence* rejection, so that path still rolled the
fix back and failed the whole monitor. Evidence now escalates on every
correction, and a contentful-but-off-path FIXED escalates to ``needs_human``
with the commit preserved instead of terminating the protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT,
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_NON_FIX_CITES_OWN_COMMIT,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    correction_unscoped_fix_outcome,
    path_level_item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_REVIEWED_PATH = "src/awf/runtime/pr_monitor_runner/ci_ops.py"
_NO_VERDICT_LINE = "Threaded the recheck through the seam; the full sweep is still running."


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=_REVIEWED_PATH,
        line=957,
        body_excerpt="recheck terminal state before CI failure returns",
    )


def _fail_unscoped_evidence_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the unscoped (``item_path=None``) evidence probe raise."""

    async def _probe(runner: object, **kwargs: object) -> bool:
        if kwargs.get("item_path") is None:
            raise OSError("git rev-parse spawn failed")
        return await path_level_item_fix_evidence(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "path_level_item_fix_evidence", _probe)


async def _address(runner: _VerdictRunner, thread: ReviewThread) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
async def test_off_anchor_fix_accepted_at_path_level_after_protocol_violation(
    tmp_path: Path,
) -> None:
    """The ws_46bc0f45 shape: protocol violation, then a legitimate off-anchor FIXED."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: rechecked terminal state before the CI-failure returns",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_protocol_violation_then_fixed"))

    assert verdict == "fix_committed"
    assert len(runner.prompts) == 2
    # The correction was for the missing verdict line, not for evidence.
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT not in runner.prompts[1]
    # The fix is kept: no rollback, HEAD stays on the attempt-0 commit.
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    events = [entry.get("event") for entry in captured]
    assert "monitor.agent_verdict_protocol_retry_rollback" not in events


@pytest.mark.unit
async def test_correction_fixed_outside_anchored_path_escalates_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A contentful commit that misses the reviewed file: needs_human, commit kept."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: fixed the shared helper in a sibling module",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_off_path_fixed"))

    assert verdict == "needs_human"
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    events = [entry.get("event") for entry in captured]
    assert "monitor.agent_verdict_protocol_retry_rollback" not in events
    escalations = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_fixed_outside_item_scope"
    ]
    assert len(escalations) == 1
    assert escalations[0]["reason_code"] == AGENT_FIXED_WITHOUT_EVIDENCE
    assert escalations[0]["item_path"] == _REVIEWED_PATH
    assert escalations[0]["attempt_tip"] == _ATTEMPT0_HEAD


@pytest.mark.unit
async def test_self_citing_false_positive_after_protocol_violation_keeps_fix(
    tmp_path: Path,
) -> None:
    """#925 D2 applies to every correction, not only the evidence-rejection one."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD} at HEAD",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_after_violation"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT


@pytest.mark.unit
async def test_fixed_without_any_change_still_terminates_after_correction(
    tmp_path: Path,
) -> None:
    """Boundary: no contentful change at all is an unsupported claim, not a misplaced fix."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: claimed without editing anything",
        ],
        heads_after_attempt=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address(runner, _thread("PRRT_no_change_fixed"))

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_unscoped_evidence_probe_failure_rolls_back_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unscoped probe reruns Git ancestry outside the sink's evidence handler.

    A transient rev-parse/ancestry failure there must not escape with the
    unaccepted correction commit still in the worktree (PRRT_kwDOSJAM6s6fpjBu).
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: fixed the shared helper in a sibling module",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )
    _fail_unscoped_evidence_probe(monkeypatch)

    with pytest.raises(OSError, match="git rev-parse spawn failed"):
        await _address(runner, _thread("PRRT_unscoped_probe_raises"))

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD


@pytest.mark.unit
async def test_unscoped_evidence_probe_failure_with_failed_rollback_is_a_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrollbackable residue after the probe failure terminates with a reason code."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: fixed the shared helper in a sibling module",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
        reset_fails=True,
    )
    _fail_unscoped_evidence_probe(monkeypatch)

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictProtocolError) as caught,
    ):
        await _address(runner, _thread("PRRT_unscoped_probe_rollback_fails"))

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert isinstance(caught.value.__cause__, OSError)
    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_unscoped_evidence_rollback_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "OSError"


@pytest.mark.unit
def test_correction_unscoped_fix_outcome_bounds_the_stored_reason() -> None:
    result = correction_unscoped_fix_outcome(
        workspace_id="ws_protocol",
        reason="x" * 2000,
        attempt_tip=_ATTEMPT0_HEAD,
        item_path=_REVIEWED_PATH,
    )
    assert result.verdict == "needs_human"
    assert len(result.reason) <= 500
    assert result.reason.endswith("…")
    assert _REVIEWED_PATH in result.reason
    assert _ATTEMPT0_HEAD[:12] in result.reason

    unknown = correction_unscoped_fix_outcome(
        workspace_id="ws_protocol",
        reason=None,
        attempt_tip=None,
        item_path=None,
    )
    assert unknown.verdict == "needs_human"
    assert "<unknown>" in unknown.reason
