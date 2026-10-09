"""A parked mutating item must not strand the batch's accepted commits (#1020, part 007).

``ws_8cd0de10`` / ``ws_6a5514fb`` / ``ws_6e4081d1`` each died mid-batch on
``AGENT_NON_FIXED_WITH_MUTATION``: three already-accepted item commits
(``2635d568``, ``74ca9457``, ``938064a2``) stayed in the worktree, never reached the
remote, and successor monitors redid the same threads. The offending item now parks
as ``needs_human`` after its rollback, so the batch runs to its end-of-batch push and
the accepted commits are published through the #937 provenance chain while the
workspace stays in ``monitoring_pr``.

The batch is pinned twice: once with the per-item verdict stubbed (what the batch
does with a parked item) and once end-to-end through the real verdict protocol, so
the mutating correction itself has to produce the park.

The last part of the file pins the mislabel the same failure event carried:
``details.operation`` said ``git push`` although no push was ever attempted.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_NON_FIXED_WITH_MUTATION,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.fix_cycle_terminal_provenance import (
    _agent_verdict_protocol_failure_result,
    _enrich_failed_fix_cycle_result,
)
from awf.runtime.pr_monitor_runner.notify_human_details import (
    _needs_human_reason_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_BASE = "a" * 40
_ITEM_HEADS = ["b" * 40, "c" * 40, "d" * 40]
_OPERATION_ID = "op_comment_repair"
_ACCEPTED_THREAD_IDS = ("PRRT_one", "PRRT_two", "PRRT_three")
_PARKED_THREAD_ID = "PRRT_four"
_MUTATION_HEAD = "f" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _thread(thread_id: str, line: int) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        path="src/app.py",
        line=line,
        body_excerpt="please fix",
        author="reviewer",
    )


def _settled_status() -> PRStatus:
    """Settle re-poll with nothing left to address, so the batch pushes."""
    return PRStatus(
        number=42,
        head_sha=_BASE,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _park_reason(reason_code: str) -> str:
    """The shape ``resolve_correction_non_fixed_mutation`` returns after rollback."""
    return (
        f"{reason_code}: Correction attempt mutated the worktree then reported a "
        f"non-FIXED verdict. The correction attempt's edits were rolled back to "
        f"{_ITEM_HEADS[2][:12]}; the item is parked for human review. "
        f"Agent verdict: false_positive. "
        f"Agent reason: the reviewer is wrong about the guard"
    )


def _assert_next_poll_hands_the_parked_item_to_a_human(state: MonitorState) -> None:
    """The park must survive the batch: no merging over the refused thread.

    Publishing the accepted commits is only half of #1020 — the parked item is
    still unresolved on the forge, and the feature (auto-merge) variant must
    answer ``NotifyHuman`` on the next otherwise-green poll instead of merging
    the PR with the refused thread open.
    """
    green_with_parked_thread = PRStatus(
        number=42,
        head_sha=_ITEM_HEADS[2],
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(_thread(_PARKED_THREAD_ID, 4),),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    action = decide(green_with_parked_thread, state, MonitorConfig(auto_merge=True))

    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason_code",
    [AGENT_NON_FIXED_WITH_MUTATION, AGENT_VERDICT_PROTOCOL_VIOLATION],
)
async def test_parked_mutating_item_still_publishes_the_batch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    """The headline #1020 regression: 3 accepted commits + a 4th parked item."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    # No ``.git`` metadata: the unpublished-repair rollback guard treats this as a
    # unit seam and leaves the batch's local commits alone.
    (worktrees_root / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()
    head = [_BASE]
    pushes: list[dict[str, object]] = []
    chain_at_push: list[list[dict[str, object]]] = []
    resolved: list[str] = []

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return head[0]

    async def _verdict(**kwargs: object) -> VerdictResult:
        item_id = str(kwargs["evidence_item_id"])
        if item_id == _PARKED_THREAD_ID:
            # Rolled back to its own floor, so HEAD does not advance.
            return VerdictResult(verdict="needs_human", reason=_park_reason(reason_code))
        head[0] = _ITEM_HEADS[_ACCEPTED_THREAD_IDS.index(item_id)]
        return VerdictResult(verdict="fix_committed", reason="fixed it")

    async def _status(**_kwargs: object) -> PRStatus:
        return _settled_status()

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _push(**_kwargs: object) -> _GitPushResult:
        pushes.append(dict(_kwargs))
        chain_at_push.append(_chain(state))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _resolve_thread(*, thread_id: str) -> None:
        resolved.append(thread_id)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _verdict)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _status)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-agent"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=tuple(
            _thread(thread_id, index + 1)
            for index, thread_id in enumerate((*_ACCEPTED_THREAD_IDS, _PARKED_THREAD_ID))
        ),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    # (c) the monitor is not failed: the loop stays in monitoring_pr and the next
    # poll answers NotifyHuman for the parked thread.
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    # (a) the accepted commits are published, exactly once, with provenance that
    # covers all three items — and the chain is retired by the push.
    assert len(pushes) == 1
    assert [record["item_id"] for record in chain_at_push[0]] == list(_ACCEPTED_THREAD_IDS)
    assert [record["head_sha"] for record in chain_at_push[0]] == _ITEM_HEADS
    assert _chain(state) == []
    assert await _persisted_chain(factory, workspace_id) == []
    # The three accepted threads are resolved on the forge; the parked one is not.
    assert resolved == list(_ACCEPTED_THREAD_IDS)
    # (b) the offending thread is parked with the reason code preserved.
    assert state.threads_addressed_ids[_PARKED_THREAD_ID] == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key(_PARKED_THREAD_ID)]
    assert reason_code in parked_reason
    assert "the reviewer is wrong about the guard" in parked_reason
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    _assert_next_poll_hands_the_parked_item_to_a_human(state)


def _chain(state: MonitorState) -> list[dict[str, object]]:
    raw = state.threads_addressed_ids.get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    if raw is None:
        return []
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    return decoded


async def _persisted_chain(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[dict[str, object]]:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    raw = (ws.monitor_threads_addressed or {}).get(_COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY)
    if raw is None:
        return []
    decoded = json.loads(raw)
    assert isinstance(decoded, list)
    return decoded


@pytest.mark.unit
def test_verdict_protocol_failure_does_not_claim_a_push_was_attempted() -> None:
    """#1020: the terminal envelope used to report ``operation=git push`` with no push."""
    failure = _agent_verdict_protocol_failure_result(
        AgentVerdictProtocolError(
            reason_code=AGENT_NON_FIXED_WITH_MUTATION,
            message="Could not roll back unaccepted edits.",
        )
    )

    evidence = failure.failure_evidence()

    assert evidence["operation"] == "agent verdict protocol"
    assert evidence["reason_code"] == AGENT_NON_FIXED_WITH_MUTATION
    assert evidence["failure_reason"] == FailureReason.agent_failure.value


@pytest.mark.unit
async def test_operation_label_survives_terminal_head_provenance_enrichment(
    tmp_path: Path,
) -> None:
    """HEAD-provenance enrichment rebuilds the envelope; the label must ride along."""

    class _Runner:
        async def _rev_parse_head(self, _worktree_path: Path) -> str:
            return "e" * 40

    failure = _agent_verdict_protocol_failure_result(
        AgentVerdictProtocolError(
            reason_code=AGENT_NON_FIXED_WITH_MUTATION,
            message="Could not roll back unaccepted edits.",
        )
    )

    enriched = await _enrich_failed_fix_cycle_result(
        _Runner(),
        failure,
        worktree_path=tmp_path,
        operation_start_head=_BASE,
    )

    evidence = enriched.failure_evidence()
    assert evidence["local_terminal_head_sha"] == "e" * 40
    assert evidence["operation"] == "agent verdict protocol"


@pytest.mark.unit
def test_an_ordinary_push_failure_still_reports_git_push() -> None:
    """Guard: the default label is unchanged for the paths that really push."""
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=128,
        stderr="remote rejected",
        reason_code="GIT_PUSH_FAILED",
    )

    assert push_result.failure_evidence()["operation"] == "git push"


class _HeadTrackingCommandRunner(FakeCommandRunner):
    """``FakeCommandRunner`` whose HEAD moves with the rollback's own reset.

    The real rollback re-reads HEAD through ``runner._deps.runner`` (not through
    ``_rev_parse_head``) and aborts when that read disagrees with the probe that
    decided the reset, so a canned empty stdout would turn every rollback into a
    terminal failure and hide the park this file is about.
    """

    def __init__(self, head: list[str]) -> None:
        super().__init__()
        self._head = head
        self.reset_targets: list[str] = []

    async def run(self, args: list[str], **kwargs: object) -> CommandResult:
        result = await super().run(args, **kwargs)  # type: ignore[arg-type]
        if "rev-parse" in args and args[-1].upper() == "HEAD":
            return CommandResult(returncode=0, stdout=f"{self._head[0]}\n", stderr="")
        if "reset" in args and "--hard" in args:
            self.reset_targets.append(args[-1])
            self._head[0] = args[-1]
        return result


@pytest.mark.unit
@pytest.mark.parametrize("pre_sink_unreadable", [False, True])
async def test_real_verdict_protocol_park_publishes_the_batch_end_to_end(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pre_sink_unreadable: bool,
) -> None:
    """The same #1020 regression driven through the real verdict protocol.

    The test above stubs ``_invoke_cli_for_verdict_result``, so it pins what the
    batch does with a ``needs_human`` item but not that the mutating correction
    produces one — it would pass against the handler that raised. Here the
    fourth item runs the real protocol: attempt 0 claims FIXED with no evidence,
    its correction attempt advances HEAD and then reports FALSE POSITIVE, and the
    refusal must park that item (``AGENT_NON_FIXED_WITH_MUTATION``, or
    ``AGENT_VERDICT_PROTOCOL_VIOLATION`` when the pre-sink HEAD probe is
    unreadable) while the three accepted item commits still reach the remote.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / workspace_id
    worktree_path.mkdir(parents=True)
    head = [_BASE]
    cmd = _HeadTrackingCommandRunner(head)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    state = MonitorState()
    pushes: list[dict[str, object]] = []
    chain_at_push: list[list[dict[str, object]]] = []
    resolved: list[str] = []
    attempts: dict[str, int] = {}
    unreadable_next_head_probe = [False]

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return (_BASE, None)

    async def _rev_parse_head(_worktree_path: Path, **_kwargs: object) -> str | None:
        if unreadable_next_head_probe[0]:
            # Exactly the correction attempt's pre-sink probe: it is the first
            # HEAD read after that attempt's agent run returns.
            unreadable_next_head_probe[0] = False
            return None
        return head[0]

    async def _agent(**kwargs: object) -> AgentRunResult:
        prompt = str(kwargs["prompt"])
        item_id = next(
            thread_id
            for thread_id in (*_ACCEPTED_THREAD_IDS, _PARKED_THREAD_ID)
            if thread_id in prompt
        )
        attempts[item_id] = attempts.get(item_id, 0) + 1
        if item_id != _PARKED_THREAD_ID:
            # An ordinary accepted item: the agent self-commits its fix.
            head[0] = _ITEM_HEADS[_ACCEPTED_THREAD_IDS.index(item_id)]
            return AgentRunResult(
                returncode=0,
                stdout="AWF-VERDICT: FIXED: fixed it",
                stderr="",
            )
        if attempts[item_id] == 1:
            # FIXED without any HEAD movement: earns the correction round.
            return AgentRunResult(
                returncode=0,
                stdout="AWF-VERDICT: FIXED: claimed without evidence",
                stderr="",
            )
        # The correction attempt mutates the worktree, then contradicts itself.
        head[0] = _MUTATION_HEAD
        unreadable_next_head_probe[0] = pre_sink_unreadable
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: the reviewer is wrong about the guard",
            stderr="",
        )

    async def _no_dirty_sink(**_kwargs: object) -> bool:
        return False

    async def _descends(*, worktree_path: Path, ancestor: str, descendant: str) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _trees_differ(*, worktree_path: Path, left: str, right: str) -> bool:
        del worktree_path
        return left != right

    async def _touches_path(**_kwargs: object) -> bool:
        return True

    async def _in_item_scope(**_kwargs: object) -> bool:
        return False

    async def _status(**_kwargs: object) -> PRStatus:
        return _settled_status()

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _push(**_kwargs: object) -> _GitPushResult:
        pushes.append(dict(_kwargs))
        chain_at_push.append(_chain(state))
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _resolve_thread(*, thread_id: str) -> None:
        resolved.append(thread_id)

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ownership_ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _agent)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_dirty_sink)
    monkeypatch.setattr(runner, "_head_descends_from", _descends)
    monkeypatch.setattr(runner, "_commit_trees_differ", _trees_differ)
    monkeypatch.setattr(runner, "_commit_range_touches_path", _touches_path)
    monkeypatch.setattr(runner, "_commit_range_in_item_scope", _in_item_scope)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _status)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-agent"),
        pr_number=42,
        pr_head_sha=_BASE,
        initial_threads=tuple(
            _thread(thread_id, index + 1)
            for index, thread_id in enumerate((*_ACCEPTED_THREAD_IDS, _PARKED_THREAD_ID))
        ),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        operation_id=_OPERATION_ID,
    )

    expected_reason_code = (
        AGENT_VERDICT_PROTOCOL_VIOLATION if pre_sink_unreadable else AGENT_NON_FIXED_WITH_MUTATION
    )
    # The old handler raised here: the batch returned a failed result and left
    # the three accepted commits in the worktree, unpushed.
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert len(pushes) == 1
    assert [record["item_id"] for record in chain_at_push[0]] == list(_ACCEPTED_THREAD_IDS)
    assert [record["head_sha"] for record in chain_at_push[0]] == _ITEM_HEADS
    assert resolved == list(_ACCEPTED_THREAD_IDS)
    # The mutating correction was really rolled back to the parked item's floor
    # (the third accepted commit), so the published range holds no refused edits.
    assert cmd.reset_targets == [_ITEM_HEADS[2]]
    assert head[0] == _ITEM_HEADS[2]
    # The item itself is parked, with the protocol's own reason code and the
    # agent's words preserved for the human who picks it up.
    assert attempts[_PARKED_THREAD_ID] == 2
    assert state.threads_addressed_ids[_PARKED_THREAD_ID] == "needs_human"
    parked_reason = state.threads_addressed_ids[_needs_human_reason_state_key(_PARKED_THREAD_ID)]
    assert expected_reason_code in parked_reason
    assert "the reviewer is wrong about the guard" in parked_reason
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    _assert_next_poll_hands_the_parked_item_to_a_human(state)
