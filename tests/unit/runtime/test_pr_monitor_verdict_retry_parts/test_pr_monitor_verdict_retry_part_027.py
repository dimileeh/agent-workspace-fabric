"""Cross-package call-site→definition FIXED evidence on the correction (issue #1019).

Two `needs_human` escalations (aira-agent PRs #1478 and #1491) were correct
fixes that landed where the behaviour is *implemented* while the reviewer
anchored where it is *observed* — a different package, so neither the path-level
(#931) nor the package-level (#952) correction gate could see them. Attempt 0
already accepts call-site→definition evidence, but only inside the reviewed
file.

On the **correction attempt only**, after the line-anchored, path-level and
package-level checks have all failed, the item's own commit range now counts as
FIXED evidence when it changes the *definition span* of a callee referenced at
the anchored line, in another file. Touching that file is not enough. Attempt 0
stays strict, the #925 D2 self-citation guard reads the widened evidence like
the earlier widenings, the #928 preserve-commit escalation keeps its reason code
for commits with no callee relationship, and the unmappable-anchor sentinel
stays fail-closed.

The runner here models Git rather than stubbing the rule: it answers
``git show <ref>:<path>``, ``git diff --name-status -z`` and ``git diff -U0 --
<path>`` from fixture dicts and delegates both the package rule and the new
callee probe to the production functions, so "cross-package callee fix",
"unrelated cross-package commit" and "file touched but not the definition" are
genuinely different inputs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from awf.common.commands import CommandResult
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import comment_verdict, comments, pre_push_validation
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_NON_FIX_CITES_OWN_COMMIT,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    callee_definition_item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_HOSTED_HEAD = "c" * 40
_NO_VERDICT_LINE = "Rewired the metric; the sweep is still running."

# PR #1478's shape: the thread is anchored on the ready-queue metrics call site
# in ``services/``, and the payload-aware fix belongs in ``observability/``.
_REVIEWED_POOLS = "src/aira_agent/services/job_pools.py"
_CALLEE_METRICS = "src/aira_agent/observability/execution_platform_metrics.py"
_CALLEE_METRICS_TEST = "tests/unit/observability/test_execution_platform_metrics.py"
_UNRELATED_MODULE = "src/aira_agent/control/queue_sweeper.py"
_ANCHOR_LINE = 6

_POOLS_TEXT = (
    "from aira_agent.observability.execution_platform_metrics import record_ready_queue_depth\n"
    "\n"
    "\n"
    "def refresh_ready_queue_metrics(pool):\n"
    "    payload = pool.snapshot()\n"
    "    record_ready_queue_depth(payload)\n"
    "    return payload\n"
)

# ``record_ready_queue_depth`` spans lines 4-9 (trailing blanks included).
_METRICS_TEXT = (
    "GAUGE = None\n"
    "\n"
    "\n"
    "def record_ready_queue_depth(payload):\n"
    '    """Record the ready-queue depth."""\n'
    "    GAUGE.set(len(payload.entries))\n"
    "    return None\n"
    "\n"
    "\n"
    "def unrelated_helper():\n"
    "    return 0\n"
)

_IN_SPAN_DIFF = (
    f"--- a/{_CALLEE_METRICS}\n"
    f"+++ b/{_CALLEE_METRICS}\n"
    "@@ -6 +6 @@\n"
    "-    GAUGE.set(len(payload.entries))\n"
    "+    GAUGE.set(payload.ready_depth())\n"
)

_OUT_OF_SPAN_DIFF = (
    f"--- a/{_CALLEE_METRICS}\n"
    f"+++ b/{_CALLEE_METRICS}\n"
    "@@ -11 +11 @@\n"
    "-    return 0\n"
    "+    return 1\n"
)

_LOCAL_CLOSURE_METRICS_TEXT = (
    "def build_recorder():\n"
    "    def record_ready_queue_depth(payload):\n"
    "        return payload\n"
    "\n"
    "    return record_ready_queue_depth\n"
)

_LOCAL_CLOSURE_DIFF = (
    f"--- a/{_CALLEE_METRICS}\n"
    f"+++ b/{_CALLEE_METRICS}\n"
    "@@ -3 +3 @@\n"
    "-        return payload\n"
    "+        return payload.entries\n"
)

_UNRELATED_TEXT = "def sweep_queues():\n    return 0\n"

_UNRELATED_DIFF = (
    f"--- a/{_UNRELATED_MODULE}\n"
    f"+++ b/{_UNRELATED_MODULE}\n"
    "@@ -2 +2 @@\n"
    "-    return 0\n"
    "+    return 1\n"
)


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


class _CalleeEvidenceRunner(_VerdictRunner):
    """A ``_VerdictRunner`` that answers the Git reads the callee probe issues.

    ``_commit_range_in_item_scope`` and
    ``_commit_range_changes_callee_definition`` both delegate to
    production code, so the package rule genuinely rejects the cross-package
    commit and the new gate genuinely has to resolve the call site.
    """

    def __init__(
        self,
        *,
        changed_paths: list[str],
        texts: dict[str, str],
        diffs: dict[str, str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.changed_paths = list(changed_paths)
        self.texts = dict(texts)
        self.diffs = dict(diffs)

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "show" in cmd:
            _ref, _, path = cmd[-1].partition(":")
            text = self.texts.get(path)
            if text is None:
                return CommandResult(returncode=128, stdout="", stderr="no such path")
            return CommandResult(returncode=0, stdout=text, stderr="")
        if "diff" in cmd and "--name-status" in cmd:
            return CommandResult(
                returncode=0, stdout=_name_status_z(*self.changed_paths), stderr=""
            )
        if "diff" in cmd and "-U0" in cmd:
            return CommandResult(returncode=0, stdout=self.diffs.get(cmd[-1], ""), stderr="")
        return await super()._run_git(cmd, **kwargs)

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

    async def _commit_range_changes_callee_definition(
        self,
        **kwargs: Any,
    ) -> bool:
        return await cross_file._commit_range_changes_callee_definition(self, **kwargs)


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(thread_id: str, path: str = _REVIEWED_POOLS, line: int = _ANCHOR_LINE) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=path,
        line=line,
        body_excerpt="use the payload-aware class in ready-queue metrics",
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


def _runner(
    tmp_path: Path,
    *,
    outputs: list[str],
    changed_paths: list[str] | None = None,
    metrics_text: str = _METRICS_TEXT,
    metrics_diff: str = _IN_SPAN_DIFF,
    dirty_after_attempt: list[bool] | None = None,
) -> _CalleeEvidenceRunner:
    (tmp_path / "ws_protocol").mkdir(exist_ok=True)
    return _CalleeEvidenceRunner(
        changed_paths=changed_paths or [_CALLEE_METRICS, _CALLEE_METRICS_TEST],
        texts={
            _REVIEWED_POOLS: _POOLS_TEXT,
            _CALLEE_METRICS: metrics_text,
            _CALLEE_METRICS_TEST: "def test_record():\n    assert True\n",
            _UNRELATED_MODULE: _UNRELATED_TEXT,
        },
        diffs={
            _CALLEE_METRICS: metrics_diff,
            _UNRELATED_MODULE: _UNRELATED_DIFF,
        },
        worktrees_root=tmp_path,
        outputs=outputs,
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=dirty_after_attempt or [True, True],
        path_touched=False,
    )


@pytest.mark.unit
async def test_cross_package_callee_fix_accepted_on_the_correction(tmp_path: Path) -> None:
    """The #1019 regression: anchor in package A, callee definition fixed in package B."""
    runner = _runner(
        tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: the metrics recorder now reads the payload",
            "AWF-VERDICT: FIXED: the recorder this line calls is where the fix belongs",
        ],
    )

    verdict = await _address(runner, _thread("thread_callee_cross_package"))

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]


@pytest.mark.unit
async def test_cross_package_commit_without_a_callee_link_still_escalates(
    tmp_path: Path,
) -> None:
    """No callee relationship keeps today's outcome: escalate, keep the commit."""
    runner = _runner(
        tmp_path,
        changed_paths=[_UNRELATED_MODULE],
        outputs=[
            "AWF-VERDICT: FIXED: changed the sweeper",
            "AWF-VERDICT: FIXED: still the sweeper",
        ],
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_no_callee_link"))

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
async def test_touching_the_definition_file_outside_the_span_still_escalates(
    tmp_path: Path,
) -> None:
    """Changing the callee's file is not evidence; the diff must hit its definition."""
    runner = _runner(
        tmp_path,
        metrics_diff=_OUT_OF_SPAN_DIFF,
        outputs=[
            "AWF-VERDICT: FIXED: edited the metrics module",
            "AWF-VERDICT: FIXED: the metrics module is where it belongs",
        ],
    )

    verdict = await _address(runner, _thread("thread_file_not_definition"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_function_local_callee_definition_still_escalates(tmp_path: Path) -> None:
    """A closure is not importable, so it cannot be the callee of a cross-file call."""
    runner = _runner(
        tmp_path,
        metrics_text=_LOCAL_CLOSURE_METRICS_TEXT,
        metrics_diff=_LOCAL_CLOSURE_DIFF,
        outputs=[
            "AWF-VERDICT: FIXED: edited the local recorder",
            "AWF-VERDICT: FIXED: the local recorder is the fix",
        ],
    )

    verdict = await _address(runner, _thread("thread_local_closure"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_attempt_zero_with_only_a_callee_fix_is_still_corrected(tmp_path: Path) -> None:
    """Attempt 0 stays strict, so a cross-package callee fix still earns its correction."""
    runner = _runner(
        tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: the metrics recorder now reads the payload",
            "AWF-VERDICT: NEEDS_HUMAN: the metric contract needs an operator call",
        ],
        dirty_after_attempt=[True, False],
    )

    verdict = await _address(runner, _thread("thread_attempt_zero_strict"))

    assert verdict == "needs_human"
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]


@pytest.mark.unit
async def test_self_citing_false_positive_with_callee_evidence_returns_fixed(
    tmp_path: Path,
) -> None:
    """The #925 D2 self-citation guard reads the widened evidence, as in #952."""
    runner = _runner(
        tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        dirty_after_attempt=[True, False],
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_self_cite_callee"))

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
async def test_self_citing_needs_human_with_callee_evidence_stays_needs_human(
    tmp_path: Path,
) -> None:
    """Widened evidence must not convert a requested human gate (issue:5558086911)."""
    runner = _runner(
        tmp_path,
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: NEEDS_HUMAN: addressed by {_ATTEMPT0_HEAD[:12]}, policy call needed",
        ],
        dirty_after_attempt=[True, False],
    )

    verdict = await _address(runner, _thread("thread_self_cite_human"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_self_citing_false_positive_without_a_callee_link_escalates(
    tmp_path: Path,
) -> None:
    """A self-citing commit with no evidence at all is still preserved and escalated."""
    runner = _runner(
        tmp_path,
        changed_paths=[_UNRELATED_MODULE],
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: FALSE POSITIVE: already addressed by commit {_ATTEMPT0_HEAD}",
        ],
        dirty_after_attempt=[True, False],
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, _thread("thread_self_cite_unrelated"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    self_citation = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]
    assert len(self_citation) == 1
    assert self_citation[0]["has_path_evidence"] is False


@pytest.mark.unit
async def test_unmappable_anchor_stays_fail_closed_with_callee_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``item_line <= 0`` sentinel outranks the widening (PRRT_kwDOSJAM6s6dFLGV)."""

    async def _no_line(*_args: object, **_kwargs: object) -> int | None:
        return None

    async def _same_path(*_args: object, **kwargs: object) -> str | None:
        return str(kwargs["path"])

    monkeypatch.setattr(ancestry, "_map_review_line_through_commits", _no_line)
    monkeypatch.setattr(ancestry, "_map_review_path_through_commits", _same_path)

    runner = _runner(
        tmp_path,
        outputs=[
            "AWF-VERDICT: FIXED: the metrics recorder now reads the payload",
            "AWF-VERDICT: FIXED: still the recorder",
        ],
        dirty_after_attempt=[True, False],
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_REVIEWED_POOLS,
        evidence_item_line=_ANCHOR_LINE,
        evidence_anchor_head=_HOSTED_HEAD,
    )

    assert result.verdict == "needs_human"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


class _CalleeProbeRunner(SimpleNamespace):
    """Minimal runner exposing only the Git seams the callee probe needs."""

    def __init__(
        self,
        *,
        head: str | None,
        non_descendants: frozenset[str] = frozenset(),
        identical_trees: frozenset[str] = frozenset(),
        callee_heads: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.head = head
        self.non_descendants = non_descendants
        self.identical_trees = identical_trees
        self.callee_heads = callee_heads
        self.callee_calls: list[str] = []

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

    async def _commit_range_changes_callee_definition(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
        item_path: str,
        item_line: int,
    ) -> bool:
        del worktree_path, left, item_path, item_line
        self.callee_calls.append(right)
        return right in self.callee_heads


async def _callee_evidence(
    runner: object,
    *,
    worktree_path: Path,
    item_start_head: str | None = _ITEM_START_HEAD,
    item_path: str | None = _REVIEWED_POOLS,
    item_line: int | None = _ANCHOR_LINE,
    state: MonitorState | None = None,
    dirty_changes_committed: bool = True,
) -> bool:
    return await callee_definition_item_fix_evidence(
        runner,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        item_path=item_path,
        item_line=item_line,
        state=state,
        dirty_changes_committed=dirty_changes_committed,
    )


@pytest.mark.unit
async def test_callee_evidence_requires_a_baseline_a_path_and_a_usable_line(
    tmp_path: Path,
) -> None:
    """Without a baseline, a path, or a mappable anchor line there is no call site."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _CalleeProbeRunner(head=_ATTEMPT0_HEAD, callee_heads=frozenset({_ATTEMPT0_HEAD}))

    assert not await _callee_evidence(runner, worktree_path=worktree, item_start_head=None)
    assert not await _callee_evidence(runner, worktree_path=worktree, item_path=None)
    assert not await _callee_evidence(runner, worktree_path=worktree, item_line=None)
    assert not await _callee_evidence(runner, worktree_path=worktree, item_line=0)
    assert not await _callee_evidence(runner, worktree_path=worktree, item_line=-1)
    assert runner.callee_calls == []


@pytest.mark.unit
async def test_callee_evidence_requires_an_existing_worktree(tmp_path: Path) -> None:
    """A missing worktree cannot be probed; the widening never invents evidence."""
    runner = _CalleeProbeRunner(head=_ATTEMPT0_HEAD, callee_heads=frozenset({_ATTEMPT0_HEAD}))

    assert not await _callee_evidence(runner, worktree_path=tmp_path / "gone")
    assert runner.callee_calls == []


@pytest.mark.unit
async def test_callee_evidence_false_for_a_runner_without_the_git_seams(tmp_path: Path) -> None:
    """Lightweight runners get ``False``, not the dirty-sink fallback of earlier gates."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _head(_worktree_path: Path) -> str | None:
        return _ATTEMPT0_HEAD

    async def _true(**_kwargs: object) -> bool:
        return True

    bare = SimpleNamespace(_rev_parse_head=_head)
    no_probe = SimpleNamespace(
        _rev_parse_head=_head,
        _head_descends_from=_true,
        _commit_trees_differ=_true,
    )

    assert not await _callee_evidence(bare, worktree_path=worktree)
    assert not await _callee_evidence(no_probe, worktree_path=worktree)


@pytest.mark.unit
async def test_callee_evidence_skips_candidates_that_are_not_forward_changes(
    tmp_path: Path,
) -> None:
    """The item's own start, a non-descendant, and an identical tree are all skipped."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    unmoved = _CalleeProbeRunner(head=_ITEM_START_HEAD, callee_heads=frozenset({_ITEM_START_HEAD}))
    assert not await _callee_evidence(unmoved, worktree_path=worktree)
    assert unmoved.callee_calls == []

    unreadable = _CalleeProbeRunner(head=None)
    assert not await _callee_evidence(unreadable, worktree_path=worktree)
    assert unreadable.callee_calls == []

    forked = _CalleeProbeRunner(
        head=_ATTEMPT0_HEAD,
        non_descendants=frozenset({_ATTEMPT0_HEAD}),
        callee_heads=frozenset({_ATTEMPT0_HEAD}),
    )
    assert not await _callee_evidence(forked, worktree_path=worktree)
    assert forked.callee_calls == []

    empty = _CalleeProbeRunner(
        head=_ATTEMPT0_HEAD,
        identical_trees=frozenset({_ATTEMPT0_HEAD}),
        callee_heads=frozenset({_ATTEMPT0_HEAD}),
    )
    assert not await _callee_evidence(empty, worktree_path=worktree)
    assert empty.callee_calls == []

    no_link = _CalleeProbeRunner(head=_ATTEMPT0_HEAD)
    assert not await _callee_evidence(no_link, worktree_path=worktree)
    assert no_link.callee_calls == [_ATTEMPT0_HEAD]


@pytest.mark.unit
async def test_callee_evidence_accepts_the_hosted_candidate_head(tmp_path: Path) -> None:
    """A hosted adapter's pushed head is a candidate too, exactly as in the strict gate."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _CalleeProbeRunner(head=_ITEM_START_HEAD, callee_heads=frozenset({_HOSTED_HEAD}))
    state = MonitorState()
    state.hosted_terminal_head_advanced = True
    state.last_push_sha = _HOSTED_HEAD

    assert await _callee_evidence(runner, worktree_path=worktree, state=state)
    assert runner.callee_calls == [_HOSTED_HEAD]

    # An advanced-but-unrecorded push adds no candidate, and a hosted head equal
    # to the worktree HEAD is not probed twice.
    blank = _CalleeProbeRunner(head=_ITEM_START_HEAD, callee_heads=frozenset({_HOSTED_HEAD}))
    blank_state = MonitorState()
    blank_state.hosted_terminal_head_advanced = True
    blank_state.last_push_sha = None
    assert not await _callee_evidence(blank, worktree_path=worktree, state=blank_state)
    assert blank.callee_calls == []

    duplicate = _CalleeProbeRunner(head=_HOSTED_HEAD, callee_heads=frozenset({_HOSTED_HEAD}))
    duplicate_state = MonitorState()
    duplicate_state.hosted_terminal_head_advanced = True
    duplicate_state.last_push_sha = _HOSTED_HEAD
    assert await _callee_evidence(duplicate, worktree_path=worktree, state=duplicate_state)
    assert duplicate.callee_calls == [_HOSTED_HEAD]
