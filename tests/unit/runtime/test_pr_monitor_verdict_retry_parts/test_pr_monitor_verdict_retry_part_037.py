"""#1105: a correction verdict that cites this item's own commit must not discard it.

A correction attempt can answer ``FALSE POSITIVE: already addressed by earlier
commit X`` where X is a commit *this item* made — attempt 0's, or one the
correction attempt itself made before reporting (observed twice on 2026-11-14).
``AGENT_NON_FIXED_WITH_MUTATION`` used to win over the self-citation rule and
roll the whole item back to ``item_start_head``, discarding the correct fix.

The self-cited commit is now treated as the FIXED claim it is: it and every item
commit under it are kept, only uncited later commits and uncommitted edits are
rolled back, and the kept commit is judged by the unchanged item-scoped FIXED
evidence rules — accepted, or parked ``needs_human`` with the commit preserved.
A citation of a pre-existing commit, or no citation at all, still parks as a
mutation exactly as before.

Heads: ``A`` item start, ``B`` attempt 0's commit, ``D`` the correction's own
commit, ``E`` AWF's dirty-sink commit of the correction's uncommitted edits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_NON_FIXED_WITH_MUTATION,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    AGENT_NON_FIX_CITES_OWN_COMMIT,
    cited_item_commit,
    item_commits_correction_context,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction_mutation import (
    keep_self_cited_item_commit,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    remember_item_start_head,
)
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_A = "a" * 40
_B = "b" * 40
_D = "d" * 40
_E = "e" * 40
_FOREIGN = "f" * 40
_REVIEWED_PATH = "src/awf/reviewed.py"
_ITEM_ID = "PRRT_self_cite_1105"
_PARTIAL_EVENT = "monitor.agent_verdict_correction_self_cite_partial_rollback"
_ATTEMPT0_FIXED = "AWF-VERDICT: FIXED: re-read the status before notifying"


class _ItemRangeRunner(_VerdictRunner):
    """``_VerdictRunner`` with a real commit graph for ancestry, rev-list and log."""

    def __init__(
        self,
        *,
        parents: dict[str, str],
        subjects: dict[str, str] | None = None,
        rev_list_fails: bool = False,
        self_commits: dict[int, str] | None = None,
        **kwargs: object,
    ) -> None:
        self._parents = parents
        self._subjects = subjects or {}
        self._rev_list_fails = rev_list_fails
        self._self_commits = self_commits or {}
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.rev_list_cmds: list[list[str]] = []
        self.log_cmds: list[list[str]] = []

    def _range(self, spec: str) -> list[str]:
        start, tip = spec.split("..")
        commits: list[str] = []
        node: str | None = tip
        while node and node != start:
            commits.append(node)
            node = self._parents.get(node)
        return commits

    async def _run_git(self, cmd: list[str], **kwargs: object) -> CommandResult:
        if "rev-list" in cmd:
            self.rev_list_cmds.append(list(cmd))
            if self._rev_list_fails:
                return CommandResult(returncode=128, stdout="", stderr="bad range")
            stdout = "".join(f"{sha}\n" for sha in self._range(cmd[-1]))
            return CommandResult(returncode=0, stdout=stdout, stderr="")
        if "log" in cmd:
            self.log_cmds.append(list(cmd))
            stdout = "".join(
                f"{sha} {self._subjects.get(sha, 'item commit')}\n" for sha in self._range(cmd[-1])
            )
            return CommandResult(returncode=0, stdout=stdout, stderr="")
        return await super()._run_git(cmd, **kwargs)

    async def _run_monitor_agent_with_service_recovery(self, **kwargs: object):  # type: ignore[no-untyped-def]
        own_commit = self._self_commits.get(self.attempt)
        if own_commit is not None:
            # The agent commits inside its run, before AWF's dirty-worktree sink.
            self.current_head = own_commit
        return await super()._run_monitor_agent_with_service_recovery(**kwargs)

    async def _head_descends_from(
        self,
        *,
        worktree_path: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        del worktree_path
        if ancestor == descendant:
            return False
        node = self._parents.get(descendant)
        while node is not None:
            if node == ancestor:
                return True
            node = self._parents.get(node)
        return False


def _runner(
    tmp_path: Path,
    *,
    correction: str,
    correction_head: str,
    correction_dirty: bool = True,
    parents: dict[str, str] | None = None,
    **kwargs: object,
) -> _ItemRangeRunner:
    (tmp_path / "ws_protocol").mkdir()
    options: dict[str, object] = {"path_touched": True, "line_touched": False}
    options.update(kwargs)
    return _ItemRangeRunner(
        worktrees_root=tmp_path,
        parents=parents or {_B: _A, _D: _B, _E: _D},
        outputs=[_ATTEMPT0_FIXED, f"AWF-VERDICT: {correction}"],
        heads_after_attempt=[_B, correction_head],
        dirty_after_attempt=[True, correction_dirty],
        **options,  # type: ignore[arg-type]
    )


async def _invoke(
    runner: _ItemRangeRunner,
    *,
    state: MonitorState | None = None,
    operation_start_head: str = _A,
    evidence_item_id: str | None = None,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=operation_start_head,
        evidence_item_id=evidence_item_id,
        evidence_item_path=_REVIEWED_PATH,
        evidence_item_line=42,
    )


def _events(captured: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [entry for entry in captured if entry.get("event") == name]


@pytest.mark.unit
async def test_self_cite_of_attempt0_commit_with_extra_edits_keeps_and_accepts_it(
    tmp_path: Path,
) -> None:
    """The cited attempt-0 commit survives; only the correction's sunk edits go."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B[:9]}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert result.reason_code is None
    assert runner.reset_targets == [_B]
    assert runner.current_head == _B
    self_citation = _events(captured, "monitor.agent_verdict_correction_cites_own_commit")
    assert len(self_citation) == 1
    assert self_citation[0]["attempt_tip"] == _B
    assert self_citation[0]["reason_code"] == AGENT_NON_FIX_CITES_OWN_COMMIT
    partial = _events(captured, _PARTIAL_EVENT)
    assert len(partial) == 1
    assert partial[0]["kept_head"] == _B
    assert partial[0]["rolled_back_from"] == _E
    assert not _events(captured, "monitor.agent_verdict_correction_mutation_item_parked")


@pytest.mark.unit
async def test_self_cite_of_attempt0_commit_without_evidence_parks_it_preserved(
    tmp_path: Path,
) -> None:
    """No item-scoped evidence: park ``needs_human`` with the commit kept (#928)."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
        path_touched=False,
        in_item_scope=False,
    )

    result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert result.reason_code is None
    assert result.rollback_head is None
    assert result.reason is not None
    assert AGENT_NON_FIXED_WITH_MUTATION not in result.reason
    assert runner.reset_targets == [_B]
    assert runner.current_head == _B


@pytest.mark.unit
async def test_self_cite_of_the_corrections_own_commit_keeps_and_evaluates_it(
    tmp_path: Path,
) -> None:
    """2026-11-14 shape: the correction commits D, then cites D as "earlier"."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_D[:7]}",
        correction_head=_D,
        correction_dirty=False,
        self_commits={1: _D},
    )

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert runner.reset_targets == []
    assert runner.current_head == _D


@pytest.mark.unit
async def test_self_cite_of_own_commit_rolls_back_only_the_later_sunk_edits(
    tmp_path: Path,
) -> None:
    """The correction commits D, leaves edits AWF sinks to E, and cites D."""
    runner = _runner(
        tmp_path,
        correction=f"DEFER: the fix already landed in {_D}",
        correction_head=_E,
        self_commits={1: _D},
    )

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert runner.reset_targets == [_D]
    assert runner.current_head == _D
    self_citation = _events(captured, "monitor.agent_verdict_correction_cites_own_commit")
    assert self_citation[0]["attempt_tip"] == _D


@pytest.mark.unit
@pytest.mark.parametrize("cited", [_A, _FOREIGN], ids=["item_start", "foreign"])
async def test_pre_existing_citation_with_edits_still_parks_as_mutation(
    tmp_path: Path,
    cited: str,
) -> None:
    """Citing a commit outside the item's own range is unchanged."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {cited}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert result.rollback_head == _A
    assert runner.reset_targets == [_A]
    assert not _events(captured, _PARTIAL_EVENT)


@pytest.mark.unit
async def test_no_citation_with_edits_still_parks_as_mutation(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        correction="FALSE POSITIVE: the reviewer misread the diff",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )

    result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert runner.reset_targets == [_A]
    assert runner.current_head == _A


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cited", "expected_target"),
    [("c" * 40, _D), (_B, _B)],
    ids=["below_floor", "above_floor"],
)
async def test_self_cite_never_rewinds_past_the_preserved_timeout_floor(
    tmp_path: Path,
    cited: str,
    expected_target: str,
) -> None:
    """#934: a cited commit under the preserved floor keeps the floor instead.

    The timed-out run committed C then D (the preserved HEAD, the floor); the
    re-attempt's attempt 0 commits B on top. Citing C must not rewind to C.
    """
    timed_out = "c" * 40
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {cited}",
        correction_head=_E,
        parents={timed_out: _A, _D: timed_out, _B: _D, _E: _B},
    )
    runner.current_head = _D
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _A)

    result = await _invoke(runner, state=state, operation_start_head=_D, evidence_item_id=_ITEM_ID)

    assert result.verdict == "fix_committed"
    assert runner.reset_targets == [expected_target]
    assert runner.current_head == expected_target


@pytest.mark.unit
async def test_explicit_needs_human_self_cite_keeps_the_cited_commit(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        correction=f"NEEDS_HUMAN: commit {_B} is the fix but a human should confirm",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )

    result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert result.reason_code is None
    assert runner.reset_targets == [_B]
    assert runner.current_head == _B


@pytest.mark.unit
async def test_failed_partial_rollback_raises_the_mutation_reason_code(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_NON_FIXED_WITH_MUTATION
    assert runner.reset_targets == [_B]


@pytest.mark.unit
async def test_unmeasurable_mutation_with_self_cite_keeps_the_violation_park(
    tmp_path: Path,
) -> None:
    """An unreadable pre-sink HEAD stays ``AGENT_VERDICT_PROTOCOL_VIOLATION``."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )
    original_agent = runner._run_monitor_agent_with_service_recovery
    original_rev_parse = runner._rev_parse_head
    fail_next_head_read: list[bool] = []

    async def _agent(**kwargs: object):  # type: ignore[no-untyped-def]
        result = await original_agent(**kwargs)
        if runner.attempt == 2:
            fail_next_head_read.append(True)
        return result

    async def _rev_parse(worktree_path: Path) -> str | None:
        if fail_next_head_read:
            fail_next_head_read.clear()
            return None
        return await original_rev_parse(worktree_path)

    runner._run_monitor_agent_with_service_recovery = _agent  # type: ignore[method-assign]
    runner._rev_parse_head = _rev_parse  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason
    assert runner.reset_targets == [_A]
    assert not _events(captured, _PARTIAL_EVENT)


@pytest.mark.unit
async def test_hosted_remote_advance_keeps_the_cited_commit_on_the_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s63brOm: a hosted agent already published E before the verdict.

    The remote is rewound to the cited B — not to item start A — and push
    tracking follows it, so the published fix survives.
    """
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*_args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )
    runner._deps.adapter.is_hosted = True

    state = MonitorState(last_push_sha=_A)

    with structlog.testing.capture_logs() as captured:
        result = await _invoke(runner, state=state)

    assert result.verdict == "fix_committed"
    assert runner.reset_targets == [_B]
    assert [call["rollback_target_sha"] for call in remote_rollbacks] == [_B]
    assert [call["expected_remote_head_sha"] for call in remote_rollbacks] == [_E]
    assert state.last_push_sha == _B
    assert state.hosted_terminal_head_advanced is True
    assert _events(captured, _PARTIAL_EVENT)


@pytest.mark.unit
async def test_unlistable_range_falls_back_to_the_known_item_heads(tmp_path: Path) -> None:
    """``rev-list`` fails: the correction-start head (B) is still this item's."""
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
        rev_list_fails=True,
    )

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert runner.reset_targets == [_B]
    assert runner.rev_list_cmds
    assert "--topo-order" in runner.rev_list_cmds[-1]


@pytest.mark.unit
async def test_correction_prompt_names_this_items_own_commits(tmp_path: Path) -> None:
    runner = _runner(
        tmp_path,
        correction="FALSE POSITIVE: the reviewer misread the diff",
        correction_head=_E,
        parents={_B: _A, _E: _B},
        subjects={_B: "fix: re-read status before notifying"},
    )

    await _invoke(runner)

    assert len(runner.prompts) == 2
    assert _B[:12] not in runner.prompts[0]
    assert f"{_B[:12]} fix: re-read status before notifying" in runner.prompts[1]
    assert "not pre-existing" in runner.prompts[1]
    assert "report FIXED" in runner.prompts[1]


@pytest.mark.unit
def test_cited_item_commit_picks_the_newest_cited_item_commit() -> None:
    candidates = [_E, _D, _B]

    def _cited(reason: str | None, **kwargs: object) -> str | None:
        options: dict[str, object] = {"candidates": candidates, "item_start_head": _A}
        options.update(kwargs)
        return cited_item_commit(reason, **options)  # type: ignore[arg-type]

    assert _cited(f"see {_B[:7]} and {_D[:8]}") == _D
    assert _cited(f"see {_B}") == _B
    # Item start and foreign commits are never item commits.
    assert _cited(f"see {_A}", candidates=[*candidates, _A]) is None
    assert _cited(f"see {_FOREIGN}") is None
    # Too short to be a commit reference.
    assert _cited(f"see {_B[:6]}") is None
    assert _cited(None) is None
    assert _cited(f"see {_B}", candidates=["", _B.upper()], item_start_head=None) == _B.upper()


@pytest.mark.unit
async def test_item_commits_correction_context_redacts_and_bounds_subjects(
    tmp_path: Path,
) -> None:
    secret = "ghp_" + "x" * 36
    runner = _runner(
        tmp_path,
        correction="unused",
        correction_head=_E,
        parents={_B: _A, _D: _B},
        subjects={_B: f"fix: token {secret}", _D: "y" * 300},
    )

    block = await item_commits_correction_context(
        runner,  # type: ignore[arg-type]
        worktree_path=tmp_path / "ws_protocol",
        item_start_head=_A,
        attempt_tip=_D,
    )

    assert f"- {_B[:12]} fix: token" in block
    assert secret not in block
    assert "y" * 121 not in block
    assert "y" * 100 in block
    assert block.index(_D[:12]) < block.index(_B[:12])


@pytest.mark.unit
async def test_item_commits_correction_context_is_empty_when_unknown(tmp_path: Path) -> None:
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A})
    worktree = tmp_path / "ws_protocol"

    async def _context(**kwargs: object) -> str:
        options: dict[str, object] = {
            "worktree_path": worktree,
            "item_start_head": _A,
            "attempt_tip": _B,
        }
        options.update(kwargs)
        return await item_commits_correction_context(runner, **options)  # type: ignore[arg-type]

    assert await _context(attempt_tip=None) == ""
    assert await _context(item_start_head=None) == ""
    assert await _context(attempt_tip=_A) == ""
    assert await _context(worktree_path=tmp_path / "missing") == ""
    assert await _context() != ""

    async def _failing_git(cmd: list[str], **_kwargs: object) -> CommandResult:
        del cmd
        return CommandResult(returncode=128, stdout="", stderr="bad range")

    runner._deps.runner.run = _failing_git
    assert await _context() == ""

    async def _empty_git(cmd: list[str], **_kwargs: object) -> CommandResult:
        del cmd
        return CommandResult(returncode=0, stdout="\n", stderr="")

    runner._deps.runner.run = _empty_git
    assert await _context() == ""

    async def _raising_git(cmd: list[str], **_kwargs: object) -> CommandResult:
        raise OSError("git spawn failed")

    runner._deps.runner.run = _raising_git
    assert await _context() == ""


async def _keep(
    runner: _ItemRangeRunner,
    *,
    reason: str | None,
    state: MonitorState | None = None,
    rollback_floor_head: str | None = _A,
) -> comment_verdict.VerdictResult | None:
    return await keep_self_cited_item_commit(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        worktree_path=runner._worktrees_root / "ws_protocol",
        item_start_head=_A,
        rollback_floor_head=rollback_floor_head,
        item_start_last_push_sha=_A,
        state=state,
        post_attempt_head=_E,
        known_item_heads=(_B, None),
        verdict="false_positive",
        reason=reason,
        item_path=_REVIEWED_PATH,
        item_line=42,
        comment_text=None,
        anchor_head=None,
    )


@pytest.mark.unit
async def test_keep_self_cited_item_commit_needs_a_reason(tmp_path: Path) -> None:
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A, _E: _B})
    runner.current_head = _E

    assert await _keep(runner, reason=None) is None
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_keep_self_cited_item_commit_hosted_without_remote_advance_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted run whose push state never moved rewinds the remote to the kept commit."""
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*_args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A, _E: _B})
    runner._deps.adapter.is_hosted = True
    runner.current_head = _E

    result = await _keep(
        runner, reason=f"already addressed by {_B}", state=MonitorState(last_push_sha=_A)
    )

    assert result is not None
    assert result.verdict == "fix_committed"
    assert runner.reset_targets == [_B]
    assert [call["rollback_target_sha"] for call in remote_rollbacks] == [_B]


@pytest.mark.unit
async def test_keep_self_cited_item_commit_hosted_published_tip_keeps_push_tracking(
    tmp_path: Path,
) -> None:
    """The hosted remote already sits at the cited commit: no rewind, tracking stays."""
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A, _E: _B})
    runner._deps.adapter.is_hosted = True
    runner.current_head = _B
    state = MonitorState(last_push_sha=_B)
    state.hosted_terminal_head_advanced = True

    result = await _keep(runner, reason=f"already addressed by {_B}", state=state)

    assert result is not None
    assert result.verdict == "fix_committed"
    assert runner.reset_targets == []
    assert state.last_push_sha == _B
    assert state.hosted_terminal_head_advanced is True


@pytest.mark.unit
async def test_keep_self_cited_item_commit_fails_closed_on_floor_probe_error(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A, _E: _B})
    runner.current_head = _E

    async def _ancestry_unreadable(**_kwargs: object) -> bool:
        raise OSError("git merge-base spawn failed")

    runner._head_descends_from = _ancestry_unreadable  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as captured:
        result = await _keep(runner, reason=f"already addressed by {_B}", rollback_floor_head=_D)

    assert result is None
    assert runner.reset_targets == []
    assert _events(captured, "monitor.agent_verdict_correction_self_cite_floor_unreadable")


@pytest.mark.unit
async def test_keep_self_cited_item_commit_refuses_a_side_branch_commit_off_the_item_start(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s63brGg: never reset onto a cited commit that drops the item start.

    E merges B with side commit S, which forked from P before item start A. The
    full-DAG ``A..E`` range lists S, but resetting onto it would discard A's
    history (earlier accepted items' fixes), so the ordinary mutation park wins.
    """
    side = "5" * 40
    fork_point = "9" * 40
    runner = _runner(
        tmp_path,
        correction="unused",
        correction_head=_E,
        parents={_A: fork_point, side: fork_point, _B: _A, _E: _B},
    )
    runner._range = lambda _spec: [_E, side, _B]  # type: ignore[method-assign]
    runner.current_head = _E

    with structlog.testing.capture_logs() as captured:
        result = await _keep(runner, reason=f"already addressed by {side}")

    assert result is None
    assert runner.reset_targets == []
    assert not _events(captured, _PARTIAL_EVENT)


@pytest.mark.unit
async def test_keep_self_cited_item_commit_parks_kept_commit_on_evidence_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s63brOn: an evidence re-check failure parks this item, not the batch."""
    runner = _runner(tmp_path, correction="unused", correction_head=_E, parents={_B: _A, _E: _B})
    runner.current_head = _E

    async def _evidence_unreadable(*_args: object, **_kwargs: object) -> bool:
        raise OSError("git diff spawn failed")

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _evidence_unreadable)

    with structlog.testing.capture_logs() as captured:
        result = await _keep(runner, reason=f"already addressed by {_B}")

    assert result is not None
    assert result.verdict == "needs_human"
    assert result.kept_commit is True
    assert "OSError" in result.reason
    assert "git diff spawn failed" in result.reason
    assert runner.reset_targets == [_B]
    assert _events(captured, "monitor.agent_verdict_correction_self_cite_evidence_unreadable")


@pytest.mark.unit
async def test_cancellation_during_kept_commit_evidence_check_keeps_the_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 6874707425: a worker cancelled during the re-check must not delete B.

    The cancellation handler rolls back only work above the kept commit, never to
    the item-start floor.
    """
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )

    item_fix_evidence = comment_verdict._item_fix_evidence

    async def _cancelled(*args: object, **kwargs: object) -> bool:
        if runner.reset_targets:
            # Only the re-check after the partial rollback is cancelled.
            raise asyncio.CancelledError
        return await item_fix_evidence(*args, **kwargs)

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert runner.reset_targets == [_B]
    assert runner.current_head == _B


@pytest.mark.unit
async def test_hosted_cancellation_after_partial_rollback_keeps_remote_and_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted: the remote already rewound to B stays there, and tracking stays on B."""
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*_args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    item_fix_evidence = comment_verdict._item_fix_evidence

    async def _cancelled(*args: object, **kwargs: object) -> bool:
        if runner.reset_targets:
            # Only the re-check after the partial rollback is cancelled.
            raise asyncio.CancelledError
        return await item_fix_evidence(*args, **kwargs)

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )
    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _cancelled)
    runner = _runner(
        tmp_path,
        correction=f"FALSE POSITIVE: already addressed by earlier commit {_B}",
        correction_head=_E,
        parents={_B: _A, _E: _B},
    )
    runner._deps.adapter.is_hosted = True
    state = MonitorState(last_push_sha=_A)

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner, state=state)

    assert runner.reset_targets == [_B]
    assert [call["rollback_target_sha"] for call in remote_rollbacks] == [_B]
    assert state.last_push_sha == _B
