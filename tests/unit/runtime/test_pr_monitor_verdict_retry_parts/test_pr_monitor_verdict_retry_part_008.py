"""#925: the FIXED-without-evidence correction path must not discard real fixes.

Attempt 0 commits a change the line-anchored gate rejects
(``AGENT_FIXED_WITHOUT_EVIDENCE``), and the correction prompt used to steer the
agent into ``FALSE POSITIVE — already addressed by commit <its own attempt-0
sha>``. The protocol then accepted that verdict and rolled the fix back, leaving
the thread dispositioned but unresolved.

These tests pin the corrected policy: a non-FIXED correction verdict
(``false_positive``, ``defer``, or ``needs_human``) that cites this item's own
attempt-0 commit never causes a rollback (commit preserved; ``needs_human``
without item-scoped related-line evidence, ``fix_committed`` when related-line
evidence is present). Path membership alone must not escalate to
``fix_committed`` — related off-anchor fixes are accepted by the line-scoped
gate (near-anchor / callee), not by discarding the line constraint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import structlog

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
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
    AGENT_FIXED_WITHOUT_EVIDENCE,
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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_merge_topology_worktree(worktree: Path) -> tuple[str, str, str]:
    """Build ``start → (fix on side) → merge tip``; fix is second-parent only.

    Returns ``(item_start_head, fix_sha, merge_tip)``. With ``--first-parent``,
    ``rev-list start..tip`` would omit ``fix_sha``.
    """
    worktree.mkdir(parents=True, exist_ok=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "base.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", "base.txt")
    _git(worktree, "commit", "-qm", "item start")
    item_start = _git(worktree, "rev-parse", "HEAD")
    _git(worktree, "checkout", "-qb", "fix-side")
    (worktree / "fix.txt").write_text("fix\n", encoding="utf-8")
    _git(worktree, "add", "fix.txt")
    _git(worktree, "commit", "-qm", "agent fix on side branch")
    fix_sha = _git(worktree, "rev-parse", "HEAD")
    _git(worktree, "checkout", "-q", "-")
    _git(worktree, "merge", "--no-ff", "-m", "merge fix into tip", "fix-side")
    merge_tip = _git(worktree, "rev-parse", "HEAD")
    first_parent_only = _git(
        worktree, "rev-list", "--first-parent", f"{item_start}..{merge_tip}"
    ).splitlines()
    assert fix_sha not in first_parent_only
    assert fix_sha in _git(worktree, "rev-list", f"{item_start}..{merge_tip}").splitlines()
    return item_start, fix_sha, merge_tip


class _RangeAwareVerdictRunner(_VerdictRunner):
    """Runner whose attempt-0 range holds a fix commit under the sink tip."""

    def __init__(self, *, attempt_commits: list[str], **kwargs: object) -> None:
        self._attempt_commits = attempt_commits
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.rev_list_ranges: list[str] = []
        self.rev_list_cmds: list[list[str]] = []

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "rev-list" in cmd:
            self.rev_list_cmds.append(list(cmd))
            self.rev_list_ranges.append(cmd[-1])
            return CommandResult(
                returncode=0,
                stdout="".join(f"{sha}\n" for sha in self._attempt_commits),
                stderr="",
            )
        return await super()._run_git(cmd, **kwargs)


class _RealRevListVerdictRunner(_VerdictRunner):
    """Delegate ``rev-list`` to a real git worktree; keep other git mocked."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.rev_list_cmds: list[list[str]] = []
        self._real_git = AsyncioSubprocessRunner()

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "rev-list" in cmd:
            self.rev_list_cmds.append(list(cmd))
            return await self._real_git.run(list(cmd), **kwargs)  # type: ignore[arg-type]
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
async def test_unrelated_same_file_edit_not_accepted_as_fix_after_evidence_correction(
    tmp_path: Path,
) -> None:
    """Path membership alone must not produce ``fix_committed`` (issue:5558086911).

    After the line-anchored gate rejects FIXED, a contentful edit elsewhere in
    the reviewed file is not item-scoped evidence. Related off-anchor fixes
    (near-anchor / callee) already pass the line-scoped gate; discarding the
    line constraint would let an unrelated same-file edit resolve the thread.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: renamed an unrelated helper in the same file",
            "AWF-VERDICT: FIXED: still only the unrelated helper",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _address(runner, _thread("thread_unrelated_same_file"))

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert len(runner.prompts) == 2
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT in runner.prompts[1]


@pytest.mark.unit
async def test_correction_false_positive_citing_own_commit_keeps_fix_and_escalates(
    tmp_path: Path,
) -> None:
    """#925 D2: self-citing FALSE POSITIVE keeps the commit; path-only ≠ FIXED."""
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

    assert verdict == "needs_human"
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
    assert self_citation[0]["has_path_evidence"] is False


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
async def test_correction_needs_human_citing_own_commit_without_path_evidence_escalates(
    tmp_path: Path,
) -> None:
    """Self-citing NEEDS_HUMAN must not roll back attempt 0's fix (#925 D2)."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: adjusted the caller in a sibling module",
            f"AWF-VERDICT: NEEDS_HUMAN: already addressed by {_ATTEMPT0_HEAD[:12]} but policy is unclear",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_needs_human_no_evidence"))

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
    assert warnings[0]["verdict"] == "needs_human"
    assert warnings[0]["has_path_evidence"] is False


@pytest.mark.unit
async def test_correction_needs_human_citing_own_commit_with_related_line_keeps_fix(
    tmp_path: Path,
) -> None:
    """Self-citing NEEDS_HUMAN with related-line evidence → fix_committed.

    Attempt 0 lacks related-line evidence (enters FIXED_WITHOUT_EVIDENCE
    correction); the correction-time evidence probe then sees related-line
    touch. Citing the attempt-0 tip must preserve the commit as FIXED rather
    than rolling it back.
    """
    (tmp_path / "ws_protocol").mkdir()

    class _EvidenceAppearsOnCorrection(_VerdictRunner):
        async def _commit_range_touches_path(self, **kwargs: object) -> bool:
            if not self.path_touched:
                return False
            line = kwargs.get("line")
            if line is not None:
                # After attempt 0, ``self.attempt == 1`` → no line evidence.
                # After the correction agent run, ``self.attempt == 2`` → yes.
                return self.attempt > 1
            return True

    runner = _EvidenceAppearsOnCorrection(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: NEEDS_HUMAN: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("PRRT_self_cite_needs_human_with_evidence"))

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
    assert self_citation[0]["verdict"] == "needs_human"
    assert self_citation[0]["has_path_evidence"] is True


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
    assert escalated.reason is not None
    assert escalated.reason.endswith("Agent reason: short")


@pytest.mark.unit
async def test_unmappable_anchor_line_stays_fail_closed_on_the_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unverifiable anchors stay fail-closed on both attempts (PRRT_kwDOSJAM6s6dFLGV).

    When the review line cannot be mapped from the anchor head onto item-start
    history the remap records the ``-1`` sentinel. That is "we cannot tell where
    this item lives", not related-line evidence, so both attempts stay strict.
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

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
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

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
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

    Attempt 0 lands a change as its own commit and the dirty-worktree sink
    commits the leftovers on top, so the verified tip is the sink commit. A
    correction verdict that cites the *fix* commit used to miss the tip-only
    comparison, roll the attempt back to item start, and discard both commits.
    Path-only touch is not item-scoped FIXED evidence; escalate to needs_human.
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

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == _ATTEMPT0_HEAD
    assert runner.rev_list_ranges == [f"{_ITEM_START_HEAD}..{_ATTEMPT0_HEAD}"]
    assert all("--first-parent" not in cmd for cmd in runner.rev_list_cmds)
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
    and rolled the legitimate change back — the #925 defect in a different disguise.
    Without related-line evidence the commit is preserved as ``needs_human``.
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

    assert verdict == "needs_human"
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


@pytest.mark.unit
async def test_attempt_commit_shas_includes_second_parent_fix_on_merge_tip(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fqb23: enumerate the full DAG, not first-parent only."""
    worktree = tmp_path / "ws_protocol"
    item_start, fix_sha, merge_tip = _init_merge_topology_worktree(worktree)
    runner = _RealRevListVerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: unused"],
        heads_after_attempt=[merge_tip],
    )

    shas = await comment_verdict_correction.attempt_commit_shas(
        runner,  # type: ignore[arg-type]
        worktree_path=worktree,
        item_start_head=item_start,
        attempt_tip=merge_tip,
    )

    assert fix_sha in shas
    assert merge_tip in shas
    assert all("--first-parent" not in cmd for cmd in runner.rev_list_cmds)
    assert await comment_verdict_correction.correction_reason_cites_own_item_commit(
        runner,  # type: ignore[arg-type]
        reason=f"already addressed by commit {fix_sha}",
        worktree_path=worktree,
        item_start_head=item_start,
        attempt_tip=merge_tip,
    )


@pytest.mark.unit
async def test_correction_citing_second_parent_fix_preserves_commit_no_rollback(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6fqb23: merge-tip self-citation must not discard the fix.

    Attempt 0 lands a merge tip whose fix lives only on the second parent. A
    correction that cites that fix SHA must keep the commit; rolling back to
    item start would discard the only copy of the change.
    """
    worktree = tmp_path / "ws_protocol"
    item_start, fix_sha, merge_tip = _init_merge_topology_worktree(worktree)
    runner = _RealRevListVerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: re-read the status before notifying",
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {fix_sha}",
        ],
        heads_after_attempt=[merge_tip, merge_tip],
        dirty_after_attempt=[True, False],
        path_touched=True,
        line_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address_thread(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            thread=_thread("PRRT_merge_second_parent"),
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start,
        )

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert runner.current_head == merge_tip
    assert all("--first-parent" not in cmd for cmd in runner.rev_list_cmds)
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    # Sanity: a hard reset would have moved HEAD off the merge tip.
    assert _git(worktree, "rev-parse", "HEAD") == merge_tip
    assert (worktree / "fix.txt").read_text(encoding="utf-8") == "fix\n"
