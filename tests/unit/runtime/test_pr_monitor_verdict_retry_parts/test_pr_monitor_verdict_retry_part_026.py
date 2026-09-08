"""Same-package FIXED evidence on the correction attempt (issue #952).

Eight `needs_human` escalations on 2026-09-07 (PRs #922, #934, #939) were
correct fixes that landed in a *sibling* of the reviewed file: either the
reviewer anchored on the callee while asking for the fix in the caller
(``lifecycle.py:640`` → ``merge_loop.py``), or a line-limit split had moved the
reviewed code into a new module (``comment_verdict.py:555`` →
``comment_verdict_entrypoint.py``), which makes any correct fix "off-path" by
construction.

On the **correction attempt only**, after the line-anchored and the path-level
checks both fail, the item's own commit range now counts as FIXED evidence when
it changes any file in the reviewed file's package — the existing bundle-scope
rule (same parent directory, or descendant paths). Attempt 0 stays strict,
cross-package commits keep escalating with the commit preserved, and the
unmappable-anchor sentinel stays fail-closed.

The runner here models the real package rule rather than a boolean: it carries
the paths its commit changed and delegates ``_commit_range_in_item_scope`` to
the production ``_changed_path_in_item_scope``, so "sibling", "extracted
module" and "other package" are genuinely different inputs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments, pre_push_validation
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_NON_FIX_CITES_OWN_COMMIT,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    package_level_item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_HOSTED_HEAD = "c" * 40
_NO_VERDICT_LINE = "Rewired the caller; the sweep is still running."

_MONITOR_PACKAGE = "src/awf/runtime/pr_monitor_runner"
_REVIEWED_LIFECYCLE = f"{_MONITOR_PACKAGE}/lifecycle.py"
_SIBLING_MERGE_LOOP = f"{_MONITOR_PACKAGE}/merge_loop.py"
_REVIEWED_VERDICT = f"{_MONITOR_PACKAGE}/comment_verdict.py"
_EXTRACTED_ENTRYPOINT = f"{_MONITOR_PACKAGE}/comment_verdict_entrypoint.py"
_REVIEWED_ADAPTER = "src/awf/adapters/codex.py"
_OTHER_PACKAGE = "src/awf/control/worker/manager.py"
_DOCS_ONLY = "docs/CONCEPTS.md"


class _PackageScopeRunner(_VerdictRunner):
    """A ``_VerdictRunner`` whose commit range changed ``changed_paths``.

    ``_commit_range_in_item_scope`` delegates to the production
    ``_changed_path_in_item_scope`` so the same-package rule under test is the
    real one, not a stub that answers the question it is meant to ask.
    """

    def __init__(self, *, changed_paths: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.changed_paths = list(changed_paths)

    async def _commit_range_in_item_scope(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
        item_path: str,
    ) -> bool:
        del worktree_path, left, right
        return any(
            pre_push_validation._changed_path_in_item_scope(
                item_path=item_path,
                changed_path=changed,
            )
            for changed in self.changed_paths
        )


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(thread_id: str, path: str, line: int = 640) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=path,
        line=line,
        body_excerpt="honor the ownership result in that caller",
    )


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
async def test_same_package_sibling_fix_accepted_on_the_correction(tmp_path: Path) -> None:
    """Shape 1 of #952: the reviewer anchors on the callee, the fix lands in the caller.

    ``lifecycle.py:640`` says "honor the ownership result in that caller"; the
    fix is in ``merge_loop.py``, same package. Attempt 0 is still rejected (the
    correction prompt is issued), and the re-affirmed FIXED is accepted rather
    than costing an operator decision.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[_SIBLING_MERGE_LOOP],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: honored the ownership result in the caller",
            "AWF-VERDICT: FIXED: the caller is where the fix belongs",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )

    verdict = await _address(runner, _thread("thread_callee_anchor", _REVIEWED_LIFECYCLE))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]


@pytest.mark.unit
async def test_extracted_module_fix_accepted_on_the_correction(tmp_path: Path) -> None:
    """Shape 2 of #952: a line-limit split moved the reviewed code out of the file.

    The thread is anchored on ``comment_verdict.py`` but the code it points at
    now lives in ``comment_verdict_entrypoint.py``, extracted on the same
    branch, so every correct fix is off-path by construction.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[_EXTRACTED_ENTRYPOINT],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: the reviewed block moved to the entrypoint module",
            "AWF-VERDICT: FIXED: the split moved this code, the fix is there",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )

    verdict = await _address(runner, _thread("thread_split_moved", _REVIEWED_VERDICT, line=555))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


@pytest.mark.unit
@pytest.mark.parametrize("changed_path", [_OTHER_PACKAGE, _DOCS_ONLY])
async def test_cross_package_correction_fix_still_escalates(
    tmp_path: Path,
    changed_path: str,
) -> None:
    """A commit in another package keeps today's outcome: escalate, keep the commit."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[changed_path],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: changed something elsewhere",
            "AWF-VERDICT: FIXED: still elsewhere",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, True],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_cross_package", _REVIEWED_ADAPTER))

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2
    escalations = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_fixed_outside_item_scope"
    ]
    assert len(escalations) == 1
    assert escalations[0]["reason_code"] == AGENT_FIXED_WITHOUT_EVIDENCE


@pytest.mark.unit
async def test_attempt_zero_with_only_a_sibling_change_is_still_corrected(
    tmp_path: Path,
) -> None:
    """Attempt 0 stays strict, so a same-package FIXED still earns its correction round."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[_SIBLING_MERGE_LOOP],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed it in the caller",
            "AWF-VERDICT: NEEDS_HUMAN: the ownership contract needs an operator call",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    verdict = await _address(runner, _thread("thread_attempt_zero_strict", _REVIEWED_LIFECYCLE))

    assert verdict == "needs_human"
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]


@pytest.mark.unit
async def test_self_citing_false_positive_with_same_package_evidence_returns_fixed(
    tmp_path: Path,
) -> None:
    """The #925 D2 self-citation guard reads the same widened evidence (#952)."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[_SIBLING_MERGE_LOOP],
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_self_cite_sibling", _REVIEWED_LIFECYCLE))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["has_path_evidence"] is True


@pytest.mark.unit
async def test_self_citing_needs_human_with_same_package_evidence_stays_needs_human(
    tmp_path: Path,
) -> None:
    """Widened evidence must not convert a requested human gate (issue:5558086911)."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _PackageScopeRunner(
        changed_paths=[_SIBLING_MERGE_LOOP],
        worktrees_root=tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: NEEDS_HUMAN: addressed by {_ATTEMPT0_HEAD[:12]}, policy call needed",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    verdict = await _address(runner, _thread("thread_self_cite_human", _REVIEWED_LIFECYCLE))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_unmappable_anchor_stays_fail_closed_with_same_package_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``item_line <= 0`` sentinel outranks the widening (PRRT_kwDOSJAM6s6dFLGV)."""
    (tmp_path / "ws_protocol").mkdir()

    async def _no_line(*_args: object, **_kwargs: object) -> int | None:
        return None

    async def _same_path(*_args: object, **kwargs: object) -> str | None:
        return str(kwargs["path"])

    monkeypatch.setattr(ancestry, "_map_review_line_through_commits", _no_line)
    monkeypatch.setattr(ancestry, "_map_review_path_through_commits", _same_path)

    runner = _PackageScopeRunner(
        changed_paths=[_SIBLING_MERGE_LOOP],
        worktrees_root=tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: fixed it in the caller",
            "AWF-VERDICT: FIXED: still the caller",
        ],
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=[True, False],
        path_touched=False,
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_REVIEWED_LIFECYCLE,
        evidence_item_line=640,
        evidence_anchor_head=_HOSTED_HEAD,
    )

    assert result.verdict == "needs_human"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


class _ScopeProbeRunner(SimpleNamespace):
    """Minimal runner exposing only the Git seams the widened probe needs."""

    def __init__(
        self,
        *,
        head: str | None,
        non_descendants: frozenset[str] = frozenset(),
        identical_trees: frozenset[str] = frozenset(),
        in_scope_heads: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.head = head
        self.non_descendants = non_descendants
        self.identical_trees = identical_trees
        self.in_scope_heads = in_scope_heads
        self.scope_calls: list[str] = []

    async def _rev_parse_head(self, _worktree_path: Path) -> str | None:
        return self.head

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path, ancestor
        return descendant not in self.non_descendants

    async def _commit_trees_differ(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
    ) -> bool:
        del worktree_path, left
        return right not in self.identical_trees

    async def _commit_range_in_item_scope(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
        item_path: str,
    ) -> bool:
        del worktree_path, left, item_path
        self.scope_calls.append(right)
        return right in self.in_scope_heads


async def _package_evidence(
    runner: object,
    *,
    worktree_path: Path,
    item_start_head: str | None = _ITEM_START_HEAD,
    item_path: str | None = _REVIEWED_LIFECYCLE,
    state: MonitorState | None = None,
    dirty_changes_committed: bool = True,
) -> bool:
    return await package_level_item_fix_evidence(
        runner,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        item_path=item_path,
        state=state,
        dirty_changes_committed=dirty_changes_committed,
    )


@pytest.mark.unit
async def test_package_evidence_requires_an_item_start_head_and_a_path(tmp_path: Path) -> None:
    """Without the item's own baseline or a reviewed path there is nothing to widen."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _ScopeProbeRunner(head=_ATTEMPT0_HEAD, in_scope_heads=frozenset({_ATTEMPT0_HEAD}))

    assert not await _package_evidence(runner, worktree_path=worktree, item_start_head=None)
    assert not await _package_evidence(runner, worktree_path=worktree, item_path=None)
    assert runner.scope_calls == []


@pytest.mark.unit
async def test_package_evidence_requires_an_existing_worktree(tmp_path: Path) -> None:
    """A missing worktree cannot be probed; the widening never invents evidence."""
    runner = _ScopeProbeRunner(head=_ATTEMPT0_HEAD, in_scope_heads=frozenset({_ATTEMPT0_HEAD}))

    assert not await _package_evidence(runner, worktree_path=tmp_path / "gone")
    assert runner.scope_calls == []


@pytest.mark.unit
async def test_package_evidence_false_for_a_runner_without_the_git_seams(tmp_path: Path) -> None:
    """Lightweight runners get ``False``, not the dirty-sink fallback of the earlier gates."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _head(_worktree_path: Path) -> str | None:
        return _ATTEMPT0_HEAD

    async def _true(**_kwargs: object) -> bool:
        return True

    bare = SimpleNamespace(_rev_parse_head=_head)
    no_scope = SimpleNamespace(
        _rev_parse_head=_head,
        _head_descends_from=_true,
        _commit_trees_differ=_true,
    )

    assert not await _package_evidence(bare, worktree_path=worktree)
    assert not await _package_evidence(no_scope, worktree_path=worktree)


@pytest.mark.unit
async def test_package_evidence_skips_candidates_that_are_not_forward_changes(
    tmp_path: Path,
) -> None:
    """The item's own start, a non-descendant, and an identical tree are all skipped."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    unmoved = _ScopeProbeRunner(head=_ITEM_START_HEAD, in_scope_heads=frozenset({_ITEM_START_HEAD}))
    assert not await _package_evidence(unmoved, worktree_path=worktree)
    assert unmoved.scope_calls == []

    unreadable = _ScopeProbeRunner(head=None)
    assert not await _package_evidence(unreadable, worktree_path=worktree)
    assert unreadable.scope_calls == []

    forked = _ScopeProbeRunner(
        head=_ATTEMPT0_HEAD,
        non_descendants=frozenset({_ATTEMPT0_HEAD}),
        in_scope_heads=frozenset({_ATTEMPT0_HEAD}),
    )
    assert not await _package_evidence(forked, worktree_path=worktree)
    assert forked.scope_calls == []

    empty = _ScopeProbeRunner(
        head=_ATTEMPT0_HEAD,
        identical_trees=frozenset({_ATTEMPT0_HEAD}),
        in_scope_heads=frozenset({_ATTEMPT0_HEAD}),
    )
    assert not await _package_evidence(empty, worktree_path=worktree)
    assert empty.scope_calls == []

    out_of_scope = _ScopeProbeRunner(head=_ATTEMPT0_HEAD)
    assert not await _package_evidence(out_of_scope, worktree_path=worktree)
    assert out_of_scope.scope_calls == [_ATTEMPT0_HEAD]


@pytest.mark.unit
async def test_package_evidence_accepts_the_hosted_candidate_head(tmp_path: Path) -> None:
    """A hosted adapter's pushed head is a candidate too, exactly as in the strict gate."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _ScopeProbeRunner(head=_ITEM_START_HEAD, in_scope_heads=frozenset({_HOSTED_HEAD}))
    state = MonitorState()
    state.hosted_terminal_head_advanced = True
    state.last_push_sha = _HOSTED_HEAD

    assert await _package_evidence(runner, worktree_path=worktree, state=state)
    assert runner.scope_calls == [_HOSTED_HEAD]

    # An advanced-but-unrecorded push adds no candidate, and a hosted head equal
    # to the worktree HEAD is not probed twice.
    blank = _ScopeProbeRunner(head=_ITEM_START_HEAD, in_scope_heads=frozenset({_HOSTED_HEAD}))
    blank_state = MonitorState()
    blank_state.hosted_terminal_head_advanced = True
    blank_state.last_push_sha = None
    assert not await _package_evidence(blank, worktree_path=worktree, state=blank_state)
    assert blank.scope_calls == []

    duplicate = _ScopeProbeRunner(head=_HOSTED_HEAD, in_scope_heads=frozenset({_HOSTED_HEAD}))
    duplicate_state = MonitorState()
    duplicate_state.hosted_terminal_head_advanced = True
    duplicate_state.last_push_sha = _HOSTED_HEAD
    assert await _package_evidence(duplicate, worktree_path=worktree, state=duplicate_state)
    assert duplicate.scope_calls == [_HOSTED_HEAD]
