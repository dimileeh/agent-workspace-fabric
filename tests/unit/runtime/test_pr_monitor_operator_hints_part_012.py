"""Operator-hint verdicts under #1074: evidence decides, not stdout formatting.

ws_6fc04496 printed a valid ``AWF-VERDICT: FIXED:`` record on its operator-hint
pass, followed by a test-result paragraph (fixture F1). The protocol rejected it
twice, rolled the fix back and failed the workspace. The terminal envelope also
said ``operation="git push"`` although nothing was pushed.

A hint calls the verdict protocol with ``require_fix_evidence=False``, because a
hint may be a no-code directive. On this path the tolerance therefore needs a
contentful commit made since the hint started. A commit-less ``FIXED`` must still
satisfy the strict contract, and when it does not, the hint parks instead of
failing the workspace.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.quality_gates import GrantSpec
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY, persist_operator_hint
from awf.runtime.pr_monitor import (
    AddressOperatorHint,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.pre_push_validation import (
    PRE_PUSH_VALIDATION_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._verdict_trailing_output_fixtures import F1, F3
from tests.unit.runtime.test_comment_repair_item_provenance_parts.test_comment_repair_item_provenance_part_007 import (
    _HeadTrackingCommandRunner,
)

_START_HEAD = "a" * 40
_HINT_FIX_HEAD = "b" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _hint() -> OperatorHint:
    return OperatorHint(
        reason="the pending-pair detection still skips queued retries",
        directive="make detection re-run both checks for open pending pairs",
        operation_id="op_hint_trailing_output",
        requested_at="2026-10-19T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


async def _run_cycle(
    runner: object,
    *,
    workspace_id: str,
    hint: OperatorHint,
    state: MonitorState,
    tmp_path: Path,
) -> _GitPushResult:
    return await runner._run_operator_hint_cycle(  # type: ignore[attr-defined, no-any-return]
        workspace_id=workspace_id,
        repo=RepoRef(owner="example", name="service"),
        pr_number=42,
        pr_head_sha=_START_HEAD,
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )


def _stub_cycle_preconditions(runner: object, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return (_START_HEAD, None)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)


@pytest.mark.unit
async def test_hint_protocol_failure_is_labelled_as_the_verdict_protocol(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 11: a failed rollback is still terminal, but not a ``git push``."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _protocol_failure(**_kwargs: object) -> VerdictResult:
        raise AgentVerdictProtocolError(
            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
            message=(
                "Could not roll back unaccepted edits before terminating after protocol violation."
            ),
        )

    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _protocol_failure)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert result.failure_reason is FailureReason.agent_failure
    assert result.operation == "agent verdict protocol"
    evidence = result.failure_evidence()
    assert evidence["operation"] == "agent verdict protocol"
    assert evidence["reason_code"] == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert state.pending_operator_hint == hint


def _real_protocol_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str,
    stdout: str,
    commits: bool,
    head: list[str],
    pushes: list[str],
) -> object:
    """A runner whose hint pass runs the real verdict protocol over a fake agent."""
    (tmp_path / workspace_id).mkdir()
    runner = make_runner(
        factory=factory,
        cmd=_HeadTrackingCommandRunner(head),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    _stub_cycle_preconditions(runner, monkeypatch)

    async def _rev_parse_head(_worktree_path: Path, **_kwargs: object) -> str | None:
        return head[0]

    async def _agent(**_kwargs: object) -> AgentRunResult:
        if commits:
            head[0] = _HINT_FIX_HEAD
        return AgentRunResult(returncode=0, stdout=stdout, stderr="")

    async def _no_dirty_sink(**_kwargs: object) -> bool:
        return False

    async def _descends(*, worktree_path: Path, ancestor: str, descendant: str) -> bool:
        del worktree_path
        return ancestor != descendant

    async def _trees_differ(*, worktree_path: Path, left: str, right: str) -> bool:
        del worktree_path
        return left != right

    async def _ownership_ok(**_kwargs: object) -> bool:
        return True

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _push(**_kwargs: object) -> _GitPushResult:
        pushes.append(head[0])
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ownership_ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", _agent)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_dirty_sink)
    monkeypatch.setattr(runner, "_head_descends_from", _descends)
    monkeypatch.setattr(runner, "_commit_trees_differ", _trees_differ)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push)
    return runner


@pytest.mark.unit
async def test_hint_f1_with_its_own_commit_is_pushed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 2 at cycle level (ws_6fc04496): the fix is accepted, kept and pushed."""
    workspace_id = await seed_monitoring_workspace(factory)
    head = [_START_HEAD]
    pushes: list[str] = []
    runner = _real_protocol_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        stdout=F1,
        commits=True,
        head=head,
        pushes=pushes,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.pushed is True
    assert pushes == [_HINT_FIX_HEAD]
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_commitless_hint_fixed_with_chatter_parks_instead_of_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 3 at cycle level: strict contract, and the workspace is not failed."""
    workspace_id = await seed_monitoring_workspace(factory)
    head = [_START_HEAD]
    pushes: list[str] = []
    runner = _real_protocol_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        stdout="AWF-VERDICT: FIXED: re-ran the failing CI job\nthe rerun still fails",
        commits=False,
        head=head,
        pushes=pushes,
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert pushes == []
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in (state.pending_operator_hint.status_reason or "")


def _record_less_hint_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str,
    pushes: list[str],
) -> object:
    """F3 on both attempts after a contentful commit, so the protocol keeps and parks."""
    return _real_protocol_runner(
        factory,
        tmp_path,
        monkeypatch,
        workspace_id=workspace_id,
        stdout=F3,
        commits=True,
        head=[_START_HEAD],
        pushes=pushes,
    )


@pytest.mark.unit
async def test_record_less_hint_keeps_its_commit_pushes_it_and_stays_parked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 on both attempts after a contentful commit: the kept commit is pushed.

    The hint still parks for a human, as an item does, but the preserved commit
    goes through the same guarded push a comment batch uses. Otherwise
    ``decide()`` answers ``NotifyHuman`` while the commit stays local.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    pushes: list[str] = []
    runner = _record_less_hint_runner(
        factory, tmp_path, monkeypatch, workspace_id=workspace_id, pushes=pushes
    )
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.pushed is True
    assert pushes == [_HINT_FIX_HEAD]
    assert state.last_push_sha == _HINT_FIX_HEAD
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    status_reason = state.pending_operator_hint.status_reason or ""
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in status_reason
    assert _HINT_FIX_HEAD[:12] in status_reason
    assert "preserved for human review" in status_reason


@pytest.mark.unit
async def test_kept_hint_commit_with_a_no_op_push_stays_parked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An up-to-date push does not finalize the parked hint as processed."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _record_less_hint_runner(
        factory, tmp_path, monkeypatch, workspace_id=workspace_id, pushes=[]
    )

    async def _up_to_date(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_validated_git_push_result", _up_to_date)
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.pushed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"


@pytest.mark.unit
async def test_kept_hint_commit_already_on_the_remote_stays_parked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotent no-op records the remote head but leaves the hint parked."""
    workspace_id = await seed_monitoring_workspace(factory)
    pushes: list[str] = []
    runner = _record_less_hint_runner(
        factory, tmp_path, monkeypatch, workspace_id=workspace_id, pushes=pushes
    )

    async def _on_remote(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _on_remote)
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert pushes == []
    assert state.last_push_sha == _HINT_FIX_HEAD
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"


@pytest.mark.unit
async def test_kept_hint_commit_under_an_active_grant_is_not_pushed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a finalized resume consumes a grant, so a grant-bearing park does not push."""
    workspace_id = await seed_monitoring_workspace(factory)
    pushes: list[str] = []
    runner = _record_less_hint_runner(
        factory, tmp_path, monkeypatch, workspace_id=workspace_id, pushes=pushes
    )

    async def _active_grant(_workspace_id: str) -> list[GrantSpec]:
        return [GrantSpec(path="pyproject.toml")]

    monkeypatch.setattr(runner, "_active_operator_grant_specs", _active_grant)
    hint = _hint()
    state = MonitorState(pending_operator_hint=hint)

    result = await _run_cycle(
        runner, workspace_id=workspace_id, hint=hint, state=state, tmp_path=tmp_path
    )

    assert result.failed is False
    assert result.pushed is False
    assert pushes == []
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"


@pytest.mark.unit
async def test_parked_hint_whose_kept_commit_push_fails_stays_parked_durably(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kept-commit push records the failure and still persists the park.

    The hint was parked before the push. A non-terminal push failure (pre-push
    validation on the unaccepted commit) must not leave that park only in memory,
    or a worker restart reloads the hint as ``pending`` and re-runs its agent.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _record_less_hint_runner(
        factory, tmp_path, monkeypatch, workspace_id=workspace_id, pushes=[]
    )

    async def _validation_failed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="pre-push validation failed",
            reason_code=PRE_PUSH_VALIDATION_FAILED_REASON,
        )

    monkeypatch.setattr(runner, "_validated_git_push_result", _validation_failed)
    hint = _hint()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()
    state = MonitorState(pending_operator_hint=hint)

    handled = await runner._execute(  # type: ignore[attr-defined]
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=PRStatus(
            number=42,
            head_sha=_START_HEAD,
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        ),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is False
    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)
        operation = (
            (
                await session.execute(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.type == OperationType.comment_repair.value,
                    )
                )
            )
            .scalars()
            .one()
        )
    assert persisted is not None
    assert persisted.status == WorkspaceStatus.monitoring_pr.value
    persisted_hint = json.loads(dict(persisted.monitor_threads_addressed)[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint["status"] == "needs_human"
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in persisted_hint["status_reason"]
    assert operation.status == OperationStatus.failed.value
    assert operation.result["reason_code"] == PRE_PUSH_VALIDATION_FAILED_REASON
