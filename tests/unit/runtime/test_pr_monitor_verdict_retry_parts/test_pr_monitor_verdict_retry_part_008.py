"""#925: the FIXED-without-evidence correction path must not discard real fixes.

Attempt 0 commits a real off-anchor fix in the reviewed file, the line-anchored
evidence gate rejects it (``AGENT_FIXED_WITHOUT_EVIDENCE``), and the correction
prompt used to steer the agent into ``FALSE POSITIVE — already addressed by
commit <its own attempt-0 sha>``. The protocol then accepted that verdict and
rolled the fix back, leaving the thread dispositioned but unresolved.

These tests pin the corrected policy: inside that correction path evidence
escalates to the anchored *path*, and a non-FIXED correction verdict that cites
this item's own attempt-0 commit never causes a rollback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import ReviewThread
from awf.runtime.pr_monitor_runner import (
    comment_verdict,
    comment_verdict_correction,
    comments,
)
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry as ancestry,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT,
    AGENT_NON_FIXED_WITH_MUTATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    AGENT_NON_FIX_CITES_OWN_COMMIT,
    correction_self_citation_outcome,
    verdict_reason_cites_own_commit,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_AGENT_FIX_COMMIT = "d" * 40
_REVIEWED_PATH = "src/awf/reviewed.py"


class _RangeAwareVerdictRunner(_VerdictRunner):
    """Runner whose attempt-0 range holds a fix commit under the sink tip."""

    def __init__(self, *, attempt_commits: list[str], **kwargs: object) -> None:
        self._attempt_commits = attempt_commits
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.rev_list_ranges: list[str] = []

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "rev-list" in cmd:
            self.rev_list_ranges.append(cmd[-1])
            return CommandResult(
                returncode=0,
                stdout="".join(f"{sha}\n" for sha in self._attempt_commits),
                stderr="",
            )
        return await super()._run_git(cmd, **kwargs)


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(thread_id: str) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=_REVIEWED_PATH,
        line=42,
        body_excerpt="notification uses stale status",
    )


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
async def test_off_anchor_fix_accepted_at_path_level_after_evidence_correction(
    tmp_path: Path,
) -> None:
    """#925 D1: a real fix elsewhere in the anchored file survives the correction."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: added a re-read helper above the caller",
            "AWF-VERDICT: FIXED: the helper is the fix for this item",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    verdict = await _address(runner, _thread("thread_off_anchor"))

    assert verdict == "fix_committed"
    # Attempt 0 was still rejected at the anchored line (strict first pass).
    assert len(runner.prompts) == 2
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT in runner.prompts[1]
    # The fix is kept: no rollback to the item-start head.
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD


@pytest.mark.unit
async def test_correction_false_positive_citing_own_commit_keeps_fix_and_returns_fixed(
    tmp_path: Path,
) -> None:
    """#925 D2: the exact incident shape — self-citing FALSE POSITIVE keeps the fix."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD} at HEAD",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    events = [entry.get("event") for entry in captured]
    assert "monitor.agent_verdict_protocol_retry_rollback" not in events
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["verdict"] == "false_positive"


@pytest.mark.unit
async def test_correction_defer_citing_own_commit_without_path_evidence_escalates(
    tmp_path: Path,
) -> None:
    """#925 D2: no path-level evidence → needs_human, commit still preserved."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: adjusted the caller in a sibling module",
            f"AWF-VERDICT: DEFER: superseded by {_ATTEMPT0_HEAD[:12]}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_no_evidence"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    warnings = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(warnings) == 1
    assert warnings[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert warnings[0]["verdict"] == "defer"


@pytest.mark.unit
async def test_correction_false_positive_citing_item_start_commit_still_rolls_back(
    tmp_path: Path,
) -> None:
    """Guard: citing a genuinely earlier commit is not self-citation."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: touched the file away from the anchor",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ITEM_START_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    verdict = await _address(runner, _thread("thread_earlier_commit"))

    assert verdict == "false_positive"
    assert runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_correction_that_mutates_and_cites_own_commit_still_rejected(
    tmp_path: Path,
) -> None:
    """Guard: ``AGENT_NON_FIXED_WITH_MUTATION`` keeps precedence over self-citation."""
    (tmp_path / "ws_protocol").mkdir()
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: touched the file away from the anchor",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, correction_head],
        dirty_after_attempt=[True, True],
        path_touched=True,
        line_touched=False,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address(runner, _thread("thread_mutating_correction"))

    assert caught.value.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_fixed_without_evidence_correction_context_excludes_own_item_commit() -> None:
    """#925 D2: the prompt must not invite citing this item's own retry commit."""
    context = _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT
    assert "earlier review item or commit" not in context
    assert "made for this review item" in context
    assert "does not count" in context


@pytest.mark.unit
def test_verdict_reason_cites_own_commit_matrix() -> None:
    """Only ≥7-hex tokens naming the attempt-0 tip count as self-citation."""
    tip = _ATTEMPT0_HEAD
    assert verdict_reason_cites_own_commit(
        f"already addressed by {tip}", attempt_tip=tip, item_start_head=_ITEM_START_HEAD
    )
    assert verdict_reason_cites_own_commit(
        f"superseded by {tip[:8]}", attempt_tip=tip, item_start_head=_ITEM_START_HEAD
    )
    # Too short to be a sha reference.
    assert not verdict_reason_cites_own_commit(
        f"see {tip[:6]}", attempt_tip=tip, item_start_head=_ITEM_START_HEAD
    )
    # A genuinely earlier commit.
    assert not verdict_reason_cites_own_commit(
        f"already fixed in {_ITEM_START_HEAD}", attempt_tip=tip, item_start_head=_ITEM_START_HEAD
    )
    # No verified attempt-0 tip → today's behaviour stands.
    assert not verdict_reason_cites_own_commit(
        f"already addressed by {tip}", attempt_tip=None, item_start_head=_ITEM_START_HEAD
    )
    # Attempt 0 never advanced HEAD, so there is no own commit to cite.
    assert not verdict_reason_cites_own_commit(
        f"already addressed by {_ITEM_START_HEAD}",
        attempt_tip=_ITEM_START_HEAD,
        item_start_head=_ITEM_START_HEAD,
    )
    assert not verdict_reason_cites_own_commit(
        None, attempt_tip=tip, item_start_head=_ITEM_START_HEAD
    )


@pytest.mark.unit
def test_correction_self_citation_outcome_bounds_the_stored_reason() -> None:
    """A maximal agent reason must not produce an unbounded persisted reason."""
    long_reason = "x" * 500
    fixed = correction_self_citation_outcome(
        workspace_id="ws_protocol",
        verdict="false_positive",
        reason=long_reason,
        attempt_tip=_ATTEMPT0_HEAD,
        has_path_evidence=True,
    )
    assert fixed.verdict == "fix_committed"
    # An accepted fix already flows through the ordinary publish-dependent path.
    assert fixed.preserved_unpublished_commit is False
    assert fixed.reason is not None
    assert len(fixed.reason) == 500
    assert fixed.reason.endswith("…")

    escalated = correction_self_citation_outcome(
        workspace_id="ws_protocol",
        verdict="defer",
        reason="short",
        attempt_tip=_ATTEMPT0_HEAD,
        has_path_evidence=False,
    )
    assert escalated.verdict == "needs_human"
    assert escalated.preserved_unpublished_commit is True
    assert escalated.reason is not None
    assert escalated.reason.endswith("Agent reason: short")


@pytest.mark.unit
async def test_unmappable_anchor_line_stays_fail_closed_on_the_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#925 D1 boundary: an unverifiable anchor never escalates to path level.

    When the review line cannot be mapped from the anchor head onto item-start
    history the remap records the ``-1`` sentinel (PRRT_kwDOSJAM6s6dFLGV). That
    is "we cannot tell where this item lives", not "the fix is off-anchor", so
    neither attempt accepts FIXED; the correction escalates with the commit kept.
    """
    (tmp_path / "ws_protocol").mkdir()

    async def _no_line(*_args: object, **_kwargs: object) -> int | None:
        return None

    async def _same_path(*_args: object, **kwargs: object) -> str | None:
        return str(kwargs["path"])

    monkeypatch.setattr(ancestry, "_map_review_line_through_commits", _no_line)
    monkeypatch.setattr(ancestry, "_map_review_path_through_commits", _same_path)

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: changed the reviewed file",
            "AWF-VERDICT: FIXED: still the same change",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_REVIEWED_PATH,
        evidence_item_line=42,
        evidence_anchor_head="c" * 40,
    )

    # Never accepted as FIXED. The contentful commit is preserved and the item
    # escalates rather than terminating the protocol (#925 follow-up).
    assert result.verdict == "needs_human"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


@pytest.mark.unit
def test_verdict_reason_cites_own_commit_covers_the_whole_attempt_range() -> None:
    """PRRT_kwDOSJAM6s6fmmKY: a non-tip commit from this attempt is self-citation."""
    attempt_commits = [_ATTEMPT0_HEAD, _AGENT_FIX_COMMIT]
    # The agent commit sits under the dirty-worktree sink tip.
    assert not verdict_reason_cites_own_commit(
        f"already addressed by {_AGENT_FIX_COMMIT}",
        attempt_tip=_ATTEMPT0_HEAD,
        item_start_head=_ITEM_START_HEAD,
    )
    assert verdict_reason_cites_own_commit(
        f"already addressed by {_AGENT_FIX_COMMIT}",
        attempt_tip=_ATTEMPT0_HEAD,
        item_start_head=_ITEM_START_HEAD,
        attempt_commits=attempt_commits,
    )
    # Abbreviated references still match a non-tip attempt commit.
    assert verdict_reason_cites_own_commit(
        f"superseded by {_AGENT_FIX_COMMIT[:8]}",
        attempt_tip=_ATTEMPT0_HEAD,
        item_start_head=_ITEM_START_HEAD,
        attempt_commits=attempt_commits,
    )
    # The item-start commit is still a genuinely earlier commit, even if a
    # malformed range listing includes it.
    assert not verdict_reason_cites_own_commit(
        f"already fixed in {_ITEM_START_HEAD}",
        attempt_tip=_ATTEMPT0_HEAD,
        item_start_head=_ITEM_START_HEAD,
        attempt_commits=[_ITEM_START_HEAD, _ATTEMPT0_HEAD],
    )
    # Blank/short entries never widen the comparison.
    assert not verdict_reason_cites_own_commit(
        f"see {_AGENT_FIX_COMMIT}",
        attempt_tip=_ATTEMPT0_HEAD,
        item_start_head=_ITEM_START_HEAD,
        attempt_commits=["   "],
    )


@pytest.mark.unit
async def test_correction_citing_non_tip_attempt_commit_keeps_the_fix(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fmmKY: citing the agent commit under the sink tip is self-citation.

    Attempt 0 lands the real fix as its own commit and the dirty-worktree sink
    commits the leftovers on top, so the verified tip is the sink commit. A
    correction verdict that cites the *fix* commit used to miss the tip-only
    comparison, roll the attempt back to item start, and discard both commits.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _RangeAwareVerdictRunner(
        attempt_commits=[_ATTEMPT0_HEAD, _AGENT_FIX_COMMIT],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_AGENT_FIX_COMMIT}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_non_tip"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    assert runner.rev_list_ranges == [f"{_ITEM_START_HEAD}..{_ATTEMPT0_HEAD}"]
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT


@pytest.mark.unit
async def test_correction_citing_own_commit_recovered_at_correction_start_keeps_the_fix(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fmmha: an unreadable post-attempt tip must not strand the fix.

    The post-attempt-0 tip probe returns None, so ``verified_attempt_tip`` stays
    unset, but the correction-start probe recovers the very same attempt-0 commit.
    Comparing the citation against the unset tip alone made self-citation invisible
    and rolled the legitimate fix back — the #925 defect in a different disguise.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
        # attempt-0 start, attempt-0 evidence, post-attempt tip probe (None);
        # every later probe falls through to the live head (_ATTEMPT0_HEAD),
        # including the correction-start read that recovers the tip.
        rev_parse_sequence=[_ITEM_START_HEAD, _ATTEMPT0_HEAD, None],
    )
    runner.current_head = _ITEM_START_HEAD

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_recovered_start"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["attempt_tip"] == _ATTEMPT0_HEAD


@pytest.mark.unit
async def test_correction_citing_foreign_commit_still_rolls_back(
    tmp_path: Path,
) -> None:
    """Guard: a commit outside the attempt range keeps the rollback path."""
    (tmp_path / "ws_protocol").mkdir()
    foreign_commit = "e" * 40
    runner = _RangeAwareVerdictRunner(
        attempt_commits=[_ATTEMPT0_HEAD, _AGENT_FIX_COMMIT],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: touched the file away from the anchor",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {foreign_commit}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    verdict = await _address(runner, _thread("thread_foreign_commit"))

    assert verdict == "false_positive"
    assert runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_attempt_commit_shas_returns_empty_when_git_cannot_list_the_range(
    tmp_path: Path,
) -> None:
    """A failed/unspawnable range listing falls back to the tip-only comparison."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    class _FailingRunner(_VerdictRunner):
        async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
            if "rev-list" in cmd:
                return CommandResult(returncode=128, stdout="", stderr="bad revision")
            return await super()._run_git(cmd, **kwargs)

    class _RaisingRunner(_VerdictRunner):
        async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
            if "rev-list" in cmd:
                raise OSError("git spawn failed")
            return await super()._run_git(cmd, **kwargs)

    kwargs: dict[str, object] = {
        "worktrees_root": tmp_path,
        "outputs": ["AWF-VERDICT: FIXED: change"],
        "heads_after_attempt": [_ATTEMPT0_HEAD],
    }
    for runner_cls in (_FailingRunner, _RaisingRunner):
        runner = runner_cls(**kwargs)  # type: ignore[arg-type]
        assert (
            await comment_verdict_correction.attempt_commit_shas(
                runner,  # type: ignore[arg-type]
                worktree_path=worktree,
                item_start_head=_ITEM_START_HEAD,
                attempt_tip=_ATTEMPT0_HEAD,
            )
            == []
        )
        assert not await comment_verdict_correction.correction_reason_cites_own_item_commit(
            runner,  # type: ignore[arg-type]
            reason=f"already addressed by {_AGENT_FIX_COMMIT}",
            worktree_path=worktree,
            item_start_head=_ITEM_START_HEAD,
            attempt_tip=_ATTEMPT0_HEAD,
        )

    # Unknown tip, an unadvanced attempt, and a missing worktree skip Git entirely.
    unused = _RaisingRunner(**kwargs)  # type: ignore[arg-type]
    for item_start, tip, path in (
        (_ITEM_START_HEAD, None, worktree),
        (None, _ATTEMPT0_HEAD, worktree),
        (_ITEM_START_HEAD, _ITEM_START_HEAD, worktree),
        (_ITEM_START_HEAD, _ATTEMPT0_HEAD, tmp_path / "missing"),
    ):
        assert (
            await comment_verdict_correction.attempt_commit_shas(
                unused,  # type: ignore[arg-type]
                worktree_path=path,
                item_start_head=item_start,
                attempt_tip=tip,
            )
            == []
        )
    assert not await comment_verdict_correction.correction_reason_cites_own_item_commit(
        unused,  # type: ignore[arg-type]
        reason=None,
        worktree_path=worktree,
        item_start_head=_ITEM_START_HEAD,
        attempt_tip=_ATTEMPT0_HEAD,
    )
