"""Correction-attempt evidence hardening (follow-up to #925 / PR #926).

ws_46bc0f45 (PR #922 monitor) died with ``AGENT_FIXED_WITHOUT_EVIDENCE`` on the
correction attempt: attempt 0 was a protocol violation (the agent left out the
verdict line while a background test sweep ran), attempt 1 a legitimate FIXED
whose hunks sat ~100 lines from the anchor in the anchored file. PR #926 only
escalated *after* an evidence rejection, so that path still rolled the commit
back and failed the whole monitor.

The evidence gate itself stays strict — same-file membership is not item-scoped
evidence (issue:5558086911). What changed is the disposition: on any correction
attempt, a FIXED whose contentful commit carries no item-scoped evidence
escalates to ``needs_human`` with the commit preserved instead of terminating
the protocol.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewThread
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
    preserved_correction_tip,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_CORRECTION_HEAD = "c" * 40
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
    """Make only the unscoped (``item_path=None``) evidence probe raise.

    The item-scoped call in the commit sink shares the same helper, so the
    wrapper delegates whenever an anchor path is supplied.
    """
    real_evidence = comment_verdict._item_fix_evidence

    async def _probe(runner: object, **kwargs: object) -> bool:
        if kwargs.get("item_path") is None:
            raise OSError("git rev-parse spawn failed")
        return await real_evidence(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)


async def _address(
    runner: _VerdictRunner,
    thread: ReviewThread,
    state: MonitorState | None = None,
) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
    )


@pytest.mark.unit
async def test_off_anchor_fix_after_protocol_violation_escalates_with_commit_kept(
    tmp_path: Path,
) -> None:
    """The ws_46bc0f45 shape: protocol violation, then an off-anchor FIXED.

    Same-file membership is not item-scoped evidence (issue:5558086911), so the
    claim is not accepted as FIXED — but the monitor must not die on it either.
    The commit is preserved and the item escalates to ``needs_human``.
    """
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

    state = MonitorState()
    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_protocol_violation_then_fixed"), state)

    assert verdict == "needs_human"
    assert len(runner.prompts) == 2
    # The correction was for the missing verdict line, not for evidence.
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT not in runner.prompts[1]
    # The commit is kept: no rollback, HEAD stays on the attempt-0 commit.
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    events = [entry.get("event") for entry in captured]
    assert "monitor.agent_verdict_protocol_retry_rollback" not in events
    assert "monitor.agent_verdict_correction_fixed_outside_item_scope" in events


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

    state = MonitorState()
    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_off_path_fixed"), state)

    assert verdict == "needs_human"
    # The preserved commit is unpublished, so the item must stay publish-dependent
    # in the fix cycle rather than parking on NotifyHuman (PRRT_kwDOSJAM6s6fpjBw).
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
async def test_correction_escalation_reports_the_preserved_commit_sha(
    tmp_path: Path,
) -> None:
    """The escalation must cite the commit it preserved, not the pre-correction tip.

    Attempt 0 is malformed without touching HEAD, so both the correction-start
    baseline and the verified attempt-0 tip are the pre-correction SHA; the
    commit actually kept is the one attempt 1 sinks (PRRT_kwDOSJAM6s6fpjBy).
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            "AWF-VERDICT: FIXED: fixed the shared helper in a sibling module",
        ],
        heads_after_attempt=[_ITEM_START_HEAD, _CORRECTION_HEAD],
        dirty_after_attempt=[False, True],
        path_touched=False,
    )

    state = MonitorState()
    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_preserved_tip_provenance"), state)

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _CORRECTION_HEAD
    escalations = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_fixed_outside_item_scope"
    ]
    assert len(escalations) == 1
    assert escalations[0]["attempt_tip"] == _CORRECTION_HEAD


@pytest.mark.unit
async def test_preserved_correction_tip_falls_back_when_worktree_is_gone(
    tmp_path: Path,
) -> None:
    """Provenance is best-effort: a missing worktree keeps the pre-correction tip."""
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_CORRECTION_HEAD],
    )

    tip = await preserved_correction_tip(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        worktree_path=tmp_path / "missing",
        rev_parse_head=runner._rev_parse_head,
        fallback=_ATTEMPT0_HEAD,
    )

    assert tip == _ATTEMPT0_HEAD


@pytest.mark.unit
async def test_preserved_correction_tip_falls_back_when_head_probe_fails(
    tmp_path: Path,
) -> None:
    """An unreadable or empty HEAD must degrade, never raise over the preserved fix."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_CORRECTION_HEAD],
    )

    async def _raising_rev_parse(_worktree_path: Path) -> str | None:
        raise OSError("git rev-parse spawn failed")

    async def _empty_rev_parse(_worktree_path: Path) -> str | None:
        return None

    with structlog.testing.capture_logs() as captured:
        raised = await preserved_correction_tip(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            worktree_path=tmp_path / "ws_protocol",
            rev_parse_head=_raising_rev_parse,
            fallback=_ATTEMPT0_HEAD,
        )
    empty = await preserved_correction_tip(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        worktree_path=tmp_path / "ws_protocol",
        rev_parse_head=_empty_rev_parse,
        fallback=_ATTEMPT0_HEAD,
    )

    assert raised == _ATTEMPT0_HEAD
    assert empty == _ATTEMPT0_HEAD
    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_preserved_tip_unreadable"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "OSError"


@pytest.mark.unit
async def test_self_citing_false_positive_after_protocol_violation_keeps_commit(
    tmp_path: Path,
) -> None:
    """#925 D2 applies to every correction, not only the evidence-rejection one.

    Without item-scoped related-line evidence the self-cited commit is preserved
    and the item escalates rather than resolving the thread (issue:5558086911).
    """
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

    state = MonitorState()
    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_after_violation"), state)

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["has_path_evidence"] is False


@pytest.mark.unit
async def test_self_citing_needs_human_after_protocol_violation_keeps_commit(
    tmp_path: Path,
) -> None:
    """An explicit corrected NEEDS_HUMAN self-cite is preserved on any correction.

    Widening the self-citation gate past the evidence rejection must carry the
    ``needs_human`` arm with it: the agent asks for a human while pointing at
    its own attempt-0 commit, so the commit is kept (never rolled back) and the
    escalation stays publish-dependent — related-line evidence does not convert
    a requested human gate into ``fix_committed`` (issue:5558086911,
    PRRT_kwDOSJAM6s6fpjBw).
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: NEEDS_HUMAN: addressed by {_ATTEMPT0_HEAD[:12]}, policy call needed",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=True,
    )

    state = MonitorState()
    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_needs_human_violation"), state)

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["verdict"] == "needs_human"
    # Related-line evidence is present and still does not buy ``fix_committed``.
    assert self_citation[0]["has_path_evidence"] is True


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
    # Publish-dependent: the preserved commit still has to reach the PR.
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
