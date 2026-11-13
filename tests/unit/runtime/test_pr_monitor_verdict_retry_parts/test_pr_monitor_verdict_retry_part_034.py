"""Comment-named target FIXED evidence on the correction (issue #1232).

Three ``needs_human`` escalations on 2026-11-13 were genuine fixes: the
reviewer anchored the comment at a usage site and *named* the fix target in the
comment body — a file path (optionally ``:line``) or a backticked identifier
whose definition lives elsewhere. The line-anchored, path-level (#925 D1),
package-level (#952) and callee-definition (#1019) gates all look only at the
anchor, so the #925 D2 self-citation guard escalated each one.

On the **correction attempt only**, after those four gates have failed, the
item's own commit range now counts as FIXED evidence when it changes a path the
*reviewer's* comment names (near the named line, when one is given) or the
definition of an identifier the reviewer backticks. The agent's reason and
AWF-authored replies are never evidence, an explicit corrected ``NEEDS_HUMAN``
stays ``needs_human``, attempt 0 stays strict, and the unmappable-anchor
sentinel stays fail-closed.

The runner models Git rather than stubbing the rule: it answers ``git show``,
``git diff --name-status -z`` and ``git diff -U0 -- <path>`` from fixture dicts
and delegates the package, callee and comment-named seams to production code.
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
from awf.runtime.pr_monitor_models import ReviewThreadComment
from awf.runtime.pr_monitor_runner import comment_verdict, comments, pre_push_validation
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_comment_named as comment_named,
)
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from awf.runtime.pr_monitor_runner.comment_verdict import AGENT_NON_FIX_CITES_OWN_COMMIT
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    comment_named_target_item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comments import _address_thread
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_ATTEMPT0_HEAD = "b" * 40
_HOSTED_HEAD = "c" * 40
_NO_VERDICT_LINE = "Updated the runbook; the sweep is still running."
_SELF_CITING_FALSE_POSITIVE = f"AWF-VERDICT: FALSE POSITIVE: already addressed by {_ATTEMPT0_HEAD}"

# Shape 1: anchored in a rollout module, the body names a runbook line.
_ROLLOUT = "apps/ops/core_rollout.py"
_RUNBOOK = "docs/runbooks/vm-core-update.md"
_RUNBOOK_AT_823 = f"--- a/{_RUNBOOK}\n+++ b/{_RUNBOOK}\n@@ -823 +823 @@\n-old step\n+new step\n"
_RUNBOOK_AT_10 = f"--- a/{_RUNBOOK}\n+++ b/{_RUNBOOK}\n@@ -10 +10 @@\n-old intro\n+new intro\n"

# Shape 2: anchored in a response schema, the body names a route helper's file.
_SCHEMAS = "src/app/api/schemas/responses.py"
_ROUTES = "src/app/api/routes/team.py"
_ROUTES_DIFF = (
    f"--- a/{_ROUTES}\n+++ b/{_ROUTES}\n@@ -2 +2 @@\n"
    "-    return team.load\n+    return team.active_load()\n"
)

# Shape 3: anchored in a channel service, the body backticks a tuple defined in
# another package; the fix inserts a tuple entry.
_CHANNEL = "src/app/agents_management/channel_service.py"
_INBOUND = "src/app/messaging/inbound_types.py"
_INBOUND_TEXT = '_TASK_INBOUND_TYPES = (\n    "a",\n    "b",\n)\n'
_INBOUND_INSERT = f'--- a/{_INBOUND}\n+++ b/{_INBOUND}\n@@ -3,0 +4 @@\n+    "c",\n'
_USAGE = "src/app/web/visibility.py"
_USAGE_TEXT = (
    "from app.messaging.inbound_types import _TASK_INBOUND_TYPES\n\n\n"
    "def visible(kind):\n    for t in _TASK_INBOUND_TYPES:\n        yield t\n"
)
_USAGE_DIFF = (
    f"--- a/{_USAGE}\n+++ b/{_USAGE}\n@@ -5 +5 @@\n"
    "-    for t in _TASK_INBOUND_TYPES:\n+    for t in sorted(_TASK_INBOUND_TYPES):\n"
)

_UNRELATED = "src/app/control/queue_sweeper.py"
_UNRELATED_DIFF = f"--- a/{_UNRELATED}\n+++ b/{_UNRELATED}\n@@ -2 +2 @@\n-    a\n+    b\n"

_SHAPE1_BODY = f"The rollout skips the drain; see `{_RUNBOOK}:823`, which documents it."
_SHAPE2_BODY = f"`member_workload()` in `{_ROUTES}` counts paused members."
_SHAPE3_BODY = (
    "Task messages are hidden: `_TASK_INBOUND_TYPES` lacks the kind that "
    "`_agent_message_visibility_clause()` filters on."
)


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


class _CommentNamedRunner(_VerdictRunner):
    """A ``_VerdictRunner`` that answers the Git reads the evidence probes issue."""

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

    async def _commit_range_changes_callee_definition(self, **kwargs: Any) -> bool:
        return await cross_file._commit_range_changes_callee_definition(self, **kwargs)

    async def _commit_range_changes_comment_named_target(self, **kwargs: Any) -> bool:
        return await comment_named._commit_range_changes_comment_named_target(self, **kwargs)


@pytest.fixture(autouse=True)
def _no_owned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty_owned_paths(_runner: object, _workspace_id: str) -> list[str]:
        return []

    monkeypatch.setattr(comments, "_owned_paths_for_prompt", _empty_owned_paths)


def _thread(
    thread_id: str,
    *,
    path: str,
    line: int,
    body: str,
    replies: tuple[ReviewThreadComment, ...] = (),
) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path=path,
        line=line,
        body_excerpt=body,
        comments=(ReviewThreadComment(comment_id="c1", body=body), *replies) if replies else (),
    )


def _shape1(thread_id: str, **kwargs: Any) -> ReviewThread:
    kwargs.setdefault("body", _SHAPE1_BODY)
    return _thread(thread_id, path=_ROLLOUT, line=849, **kwargs)


async def _address(runner: _VerdictRunner, thread: ReviewThread) -> str:
    return await _address_thread(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        repo=RepoRef(owner="o", name="r"),
        pr_number=1,
        thread=thread,
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=None,
        operation_start_head=_ITEM_START_HEAD,
    )


def _runner(
    tmp_path: Path,
    *,
    outputs: list[str],
    changed_paths: list[str],
    diffs: dict[str, str] | None = None,
    dirty_after_attempt: list[bool] | None = None,
) -> _CommentNamedRunner:
    (tmp_path / "ws_protocol").mkdir(exist_ok=True)
    return _CommentNamedRunner(
        changed_paths=changed_paths,
        texts={_INBOUND: _INBOUND_TEXT, _USAGE: _USAGE_TEXT},
        diffs={
            _RUNBOOK: _RUNBOOK_AT_823,
            _ROUTES: _ROUTES_DIFF,
            _INBOUND: _INBOUND_INSERT,
            _USAGE: _USAGE_DIFF,
            _UNRELATED: _UNRELATED_DIFF,
            **(diffs or {}),
        },
        worktrees_root=tmp_path,
        outputs=outputs,
        heads_after_attempt=[_ATTEMPT0_HEAD, _ATTEMPT0_HEAD],
        dirty_after_attempt=dirty_after_attempt or [True, False],
        path_touched=False,
    )


def _self_citation_logs(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_cites_own_commit"
    ]


@pytest.mark.parametrize(
    ("thread", "changed_paths"),
    [
        pytest.param(_shape1("thread_shape1"), [_RUNBOOK], id="shape1-path-and-line"),
        pytest.param(
            _thread("thread_shape2", path=_SCHEMAS, line=151, body=_SHAPE2_BODY),
            [_ROUTES],
            id="shape2-path-of-named-function",
        ),
        pytest.param(
            _thread("thread_shape3", path=_CHANNEL, line=932, body=_SHAPE3_BODY),
            [_INBOUND],
            id="shape3-backticked-definition",
        ),
    ],
)
@pytest.mark.unit
async def test_self_citing_false_positive_with_comment_named_evidence_returns_fixed(
    tmp_path: Path,
    thread: ReviewThread,
    changed_paths: list[str],
) -> None:
    """The three #1232 escalations become ``fix_committed`` with the commit kept."""
    runner = _runner(
        tmp_path,
        changed_paths=changed_paths,
        outputs=[_NO_VERDICT_LINE, _SELF_CITING_FALSE_POSITIVE],
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, thread)

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    self_citation = _self_citation_logs(captured)
    assert len(self_citation) == 1
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    assert self_citation[0]["has_path_evidence"] is True


@pytest.mark.unit
async def test_corrected_fixed_with_comment_named_evidence_is_accepted(tmp_path: Path) -> None:
    """A re-affirmed FIXED whose commit changes the named definition is accepted."""
    runner = _runner(
        tmp_path,
        changed_paths=[_INBOUND],
        outputs=[
            "AWF-VERDICT: FIXED: added the task kind",
            "AWF-VERDICT: FIXED: the tuple the reviewer named now has the kind",
        ],
        dirty_after_attempt=[True, True],
    )

    verdict = await _address(
        runner, _thread("thread_fixed", path=_CHANNEL, line=932, body=_SHAPE3_BODY)
    )

    assert verdict == "fix_committed"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


@pytest.mark.parametrize(
    ("thread", "changed_paths", "diffs"),
    [
        pytest.param(
            _shape1("thread_named_path_untouched"),
            [_UNRELATED],
            None,
            id="named-path-not-modified",
        ),
        pytest.param(
            _shape1("thread_named_line_far"),
            [_RUNBOOK],
            {_RUNBOOK: _RUNBOOK_AT_10},
            id="named-path-modified-far-from-named-line",
        ),
        pytest.param(
            _thread("thread_usage_only", path=_CHANNEL, line=932, body=_SHAPE3_BODY),
            [_USAGE],
            None,
            id="named-identifier-only-used",
        ),
        pytest.param(
            _shape1(
                "thread_awf_reply",
                body="The rollout skips the drain.",
                replies=(
                    ReviewThreadComment(
                        comment_id="c2",
                        body=f"Addressed in `{_RUNBOOK}:823`.",
                        viewer_did_author=True,
                    ),
                ),
            ),
            [_RUNBOOK],
            None,
            id="path-named-only-in-awf-reply",
        ),
    ],
)
@pytest.mark.unit
async def test_self_citing_false_positive_without_comment_named_evidence_escalates(
    tmp_path: Path,
    thread: ReviewThread,
    changed_paths: list[str],
    diffs: dict[str, str] | None,
) -> None:
    """No evidence keeps today's outcome: commit preserved, escalation stands."""
    runner = _runner(
        tmp_path,
        changed_paths=changed_paths,
        diffs=diffs,
        outputs=[_NO_VERDICT_LINE, _SELF_CITING_FALSE_POSITIVE],
    )

    with structlog.testing.capture_logs() as captured:
        verdict = await _address(runner, thread)

    assert verdict == "needs_human"
    assert runner.reset_targets == []
    self_citation = _self_citation_logs(captured)
    assert len(self_citation) == 1
    assert self_citation[0]["has_path_evidence"] is False


@pytest.mark.unit
async def test_path_named_only_in_the_agent_reason_is_not_evidence(tmp_path: Path) -> None:
    """The agent cannot vouch for itself: its reason text is never parsed."""
    runner = _runner(
        tmp_path,
        changed_paths=[_RUNBOOK],
        outputs=[
            _NO_VERDICT_LINE,
            f"{_SELF_CITING_FALSE_POSITIVE} in {_RUNBOOK}:823 (`_TASK_INBOUND_TYPES`)",
        ],
    )

    verdict = await _address(runner, _shape1("thread_reason_only", body="Drain first."))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_self_citing_needs_human_with_comment_named_evidence_stays_needs_human(
    tmp_path: Path,
) -> None:
    """Evidence must not convert a requested human gate (issue:5558086911)."""
    runner = _runner(
        tmp_path,
        changed_paths=[_RUNBOOK],
        outputs=[
            _NO_VERDICT_LINE,
            f"AWF-VERDICT: NEEDS_HUMAN: addressed by {_ATTEMPT0_HEAD[:12]}, policy call needed",
        ],
    )

    verdict = await _address(runner, _shape1("thread_self_cite_human"))

    assert verdict == "needs_human"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_attempt_zero_with_only_comment_named_evidence_is_still_corrected(
    tmp_path: Path,
) -> None:
    """Attempt 0 stays strict, so a comment-named fix still earns its correction."""
    runner = _runner(
        tmp_path,
        changed_paths=[_RUNBOOK],
        outputs=[
            "AWF-VERDICT: FIXED: updated the runbook",
            "AWF-VERDICT: NEEDS_HUMAN: the drain policy needs an operator call",
        ],
    )

    verdict = await _address(runner, _shape1("thread_attempt_zero_strict"))

    assert verdict == "needs_human"
    assert len(runner.prompts) == 2
    assert "no new item-scoped Git change" in runner.prompts[1]


@pytest.mark.unit
async def test_unmappable_anchor_stays_fail_closed_with_comment_named_evidence(
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
        changed_paths=[_RUNBOOK],
        outputs=["AWF-VERDICT: FIXED: updated the runbook", "AWF-VERDICT: FIXED: still it"],
    )

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_ROLLOUT,
        evidence_item_line=849,
        evidence_anchor_head=_HOSTED_HEAD,
        evidence_comment_text=_SHAPE1_BODY,
    )

    assert result.verdict == "needs_human"
    assert runner.reset_targets == []
    assert len(runner.prompts) == 2


# --- helper guards -----------------------------------------------------------------


class _NamedProbeRunner(SimpleNamespace):
    """Minimal runner exposing only the Git seams the comment-named probe needs."""

    def __init__(
        self,
        *,
        head: str | None,
        non_descendants: frozenset[str] = frozenset(),
        identical_trees: frozenset[str] = frozenset(),
        named_heads: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.head = head
        self.non_descendants = non_descendants
        self.identical_trees = identical_trees
        self.named_heads = named_heads
        self.named_calls: list[tuple[str, str]] = []
        self.named_anchor_heads: list[str | None] = []

    async def _rev_parse_head(self, _worktree_path: Path) -> str | None:
        return self.head

    async def _head_descends_from(
        self, *, worktree_path: Path, ancestor: str, descendant: str
    ) -> bool:
        del worktree_path, ancestor
        return descendant not in self.non_descendants

    async def _commit_trees_differ(self, *, worktree_path: Path, left: str, right: str) -> bool:
        del worktree_path, left
        return right not in self.identical_trees

    async def _commit_range_changes_comment_named_target(
        self,
        *,
        worktree_path: Path,
        left: str,
        right: str,
        comment_text: str,
        anchor_head: str | None,
    ) -> bool:
        del worktree_path, left
        self.named_calls.append((right, comment_text))
        self.named_anchor_heads.append(anchor_head)
        return right in self.named_heads


async def _named_evidence(
    runner: object,
    *,
    worktree_path: Path,
    item_start_head: str | None = _ITEM_START_HEAD,
    item_path: str | None = _ROLLOUT,
    item_line: int | None = 849,
    comment_text: str | None = _SHAPE1_BODY,
    state: MonitorState | None = None,
    anchor_head: str | None = None,
) -> bool:
    return await comment_named_target_item_fix_evidence(
        runner,  # type: ignore[arg-type]
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        item_path=item_path,
        item_line=item_line,
        comment_text=comment_text,
        state=state,
        anchor_head=anchor_head,
    )


@pytest.mark.unit
async def test_named_evidence_requires_a_baseline_a_path_text_and_a_mappable_anchor(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _NamedProbeRunner(head=_ATTEMPT0_HEAD, named_heads=frozenset({_ATTEMPT0_HEAD}))

    assert not await _named_evidence(runner, worktree_path=worktree, item_start_head=None)
    assert not await _named_evidence(runner, worktree_path=worktree, item_path=None)
    assert not await _named_evidence(runner, worktree_path=worktree, comment_text=None)
    assert not await _named_evidence(runner, worktree_path=worktree, comment_text="  \n")
    assert not await _named_evidence(runner, worktree_path=worktree, item_line=0)
    assert not await _named_evidence(runner, worktree_path=worktree, item_line=-1)
    assert runner.named_calls == []

    # A file-level thread (no line) is not the unmappable sentinel.
    assert await _named_evidence(runner, worktree_path=worktree, item_line=None)
    assert runner.named_calls == [(_ATTEMPT0_HEAD, _SHAPE1_BODY)]

    # The review's anchor head reaches the probe so named lines can be relocated.
    assert await _named_evidence(runner, worktree_path=worktree, anchor_head=_HOSTED_HEAD)
    assert runner.named_anchor_heads == [None, _HOSTED_HEAD]


@pytest.mark.unit
async def test_named_evidence_requires_an_existing_worktree_and_the_git_seams(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _NamedProbeRunner(head=_ATTEMPT0_HEAD, named_heads=frozenset({_ATTEMPT0_HEAD}))

    async def _head(_worktree_path: Path) -> str | None:
        return _ATTEMPT0_HEAD

    async def _true(**_kwargs: object) -> bool:
        return True

    no_probe = SimpleNamespace(
        _rev_parse_head=_head, _head_descends_from=_true, _commit_trees_differ=_true
    )

    assert not await _named_evidence(runner, worktree_path=tmp_path / "gone")
    assert runner.named_calls == []
    assert not await _named_evidence(SimpleNamespace(_rev_parse_head=_head), worktree_path=worktree)
    assert not await _named_evidence(no_probe, worktree_path=worktree)


@pytest.mark.unit
async def test_named_evidence_skips_candidates_that_are_not_forward_changes(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    accept = frozenset({_ATTEMPT0_HEAD, _ITEM_START_HEAD})

    unmoved = _NamedProbeRunner(head=_ITEM_START_HEAD, named_heads=accept)
    unreadable = _NamedProbeRunner(head=None, named_heads=accept)
    forked = _NamedProbeRunner(
        head=_ATTEMPT0_HEAD, non_descendants=frozenset({_ATTEMPT0_HEAD}), named_heads=accept
    )
    empty = _NamedProbeRunner(
        head=_ATTEMPT0_HEAD, identical_trees=frozenset({_ATTEMPT0_HEAD}), named_heads=accept
    )
    no_target = _NamedProbeRunner(head=_ATTEMPT0_HEAD)

    for skipped in (unmoved, unreadable, forked, empty):
        assert not await _named_evidence(skipped, worktree_path=worktree)
        assert skipped.named_calls == []
    assert not await _named_evidence(no_target, worktree_path=worktree)
    assert [right for right, _text in no_target.named_calls] == [_ATTEMPT0_HEAD]


@pytest.mark.unit
async def test_named_evidence_accepts_the_hosted_candidate_head(tmp_path: Path) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _NamedProbeRunner(head=_ITEM_START_HEAD, named_heads=frozenset({_HOSTED_HEAD}))
    state = MonitorState()
    state.hosted_terminal_head_advanced = True
    state.last_push_sha = _HOSTED_HEAD

    assert await _named_evidence(runner, worktree_path=worktree, state=state)
    assert [right for right, _text in runner.named_calls] == [_HOSTED_HEAD]

    duplicate = _NamedProbeRunner(head=_HOSTED_HEAD, named_heads=frozenset({_HOSTED_HEAD}))
    assert await _named_evidence(duplicate, worktree_path=worktree, state=state)
    assert [right for right, _text in duplicate.named_calls] == [_HOSTED_HEAD]

    blank_state = MonitorState()
    blank_state.hosted_terminal_head_advanced = True
    blank_state.last_push_sha = None
    blank = _NamedProbeRunner(head=_ITEM_START_HEAD, named_heads=frozenset({_HOSTED_HEAD}))
    assert not await _named_evidence(blank, worktree_path=worktree, state=blank_state)
    assert blank.named_calls == []
