"""Regression tests for operator remonitor hints in the PR monitor runner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import Operation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime import operator_hints
from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    mark_operator_hint_processed,
    operator_hint_processed_key,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import (
    AddressComments,
    AddressOperatorHint,
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    OperatorHint,
    PRStatus,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.remote_repair import _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _ready_status(
    *,
    head_sha: str = "abc1234567890def",
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


@pytest.mark.unit
def test_operator_hint_from_threads_does_not_mutate_threads_addressed() -> None:
    hint = OperatorHint(
        reason="fix the stale docs CTA",
        operation_id="op_hint_parse",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    threads_addressed = persist_operator_hint({"review-thread": "fix_committed"}, hint)

    parsed = operator_hints.operator_hint_from_threads(threads_addressed)

    assert parsed == hint
    assert threads_addressed["review-thread"] == "fix_committed"
    assert OPERATOR_HINT_STATE_KEY in threads_addressed


@pytest.mark.unit
def test_operator_hint_freeze_uses_canonical_runtime_state_key_helpers() -> None:
    assert (
        operator_hints._initial_review_grace_started_key
        is runner_helpers._initial_review_grace_started_key
    )
    assert (
        operator_hints._initial_review_grace_done_key
        is runner_helpers._initial_review_grace_done_key
    )
    assert (
        operator_hints._non_check_reviewer_settle_started_key
        is runner_helpers._non_check_reviewer_settle_started_key
    )
    assert (
        operator_hints._non_check_reviewer_settle_done_key
        is runner_helpers._non_check_reviewer_settle_done_key
    )


@pytest.mark.unit
def test_remonitor_elapsed_settle_head_shas_ignores_stale_head_when_current_known() -> None:
    stale_head_sha = "e" * 40
    current_head_sha = "f" * 40
    stale_done_key = operator_hints._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=stale_head_sha,
    )

    assert (
        operator_hints.remonitor_elapsed_settle_head_shas(
            {stale_done_key: "elapsed"},
            pr_number=42,
            preferred_head_sha=stale_head_sha,
            current_head_sha=current_head_sha,
        )
        == ()
    )


@pytest.mark.unit
def test_remonitor_elapsed_settle_head_shas_accepts_current_head_elapsed_marker() -> None:
    stale_head_sha = "e" * 40
    current_head_sha = "f" * 40
    current_done_key = operator_hints._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=current_head_sha,
    )

    assert operator_hints.remonitor_elapsed_settle_head_shas(
        {current_done_key: "elapsed"},
        pr_number=42,
        preferred_head_sha=stale_head_sha,
        current_head_sha=current_head_sha,
    ) == (current_head_sha,)


@pytest.mark.unit
async def test_operator_hint_action_dispatches_repair_and_clears_pending_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="the docs CTA URL 404s; correct URL is https://example.test/docs",
        operation_id="op_operator",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    calls: list[dict[str, object]] = []

    async def fake_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        calls.append(kwargs)
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", fake_operator_hint_cycle)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is False
    assert calls
    assert calls[0]["hint"] == hint
    assert state.pending_operator_hint is None

    async with factory() as session:
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

    assert operation.payload["action"] == "operator_hint_repair"
    assert operation.payload["reason_code"] == "OPERATOR_REMONITOR"
    assert operation.result["outcome"] == "operator_hint_pushed"


@pytest.mark.unit
async def test_operator_hint_repair_converts_protected_scope_diff_error_to_push_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="repair touched protected workflow",
        operation_id="op_protected_scope",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _raise_protected_scope(**_kwargs: object) -> None:
        raise ProtectedScopeDiffError("agent touched protected workflow")

    async def _protected_scope_result(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(kwargs["exc"]),
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise_protected_scope)
    monkeypatch.setattr(
        runner,
        "_protected_scope_diff_unavailable_push_result",
        _protected_scope_result,
    )

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_scope",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_scope",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.protected_scope_diff_unavailable is True
    assert captured["workspace_id"] == "ws_operator_hint_scope"
    assert captured["remote_branch"] == "awf/ws_operator_hint_scope"
    assert isinstance(captured["exc"], ProtectedScopeDiffError)
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason="agent touched protected workflow",
    )


@pytest.mark.unit
async def test_operator_hint_repair_marks_policy_block_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair would edit a protected workflow",
        operation_id="op_policy_blocked_hint",
        requested_at="2026-05-31T01:20:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _policy_blocked(**_kwargs: object) -> VerdictResult:
        raise _MonitorPolicyBlockedError("monitor policy blocked the operator hint repair")

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _policy_blocked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_policy_blocked",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_policy_blocked",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    assert result.returncode == 1
    assert result.stderr == "monitor policy blocked the operator hint repair"
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason="monitor policy blocked the operator hint repair",
    )


@pytest.mark.unit
async def test_operator_hint_repair_marks_runtime_ownership_failure_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair needs runtime ownership repair",
        operation_id="op_runtime_ownership_failed_hint",
        requested_at="2026-05-31T04:20:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _ownership_repair_failed(**_kwargs: object) -> VerdictResult:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "agent runtime ownership repair failed"
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _ownership_repair_failed)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_runtime_ownership_failed",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_runtime_ownership_failed",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.stderr == "agent runtime ownership repair failed"
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason="agent runtime ownership repair failed",
    )


@pytest.mark.unit
async def test_operator_hint_repair_marks_protected_scope_push_blocked_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair would edit a protected workflow",
        operation_id="op_protected_scope_blocked_hint",
        requested_at="2026-05-31T01:35:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _protected_scope_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="operator hint repair touched protected workflow",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        )

    async def _blocked_protected_scope_repair(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="operator hint repair touched protected workflow",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_scope_block)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_commits_before_push",
        _blocked_protected_scope_repair,
    )

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_protected_scope_blocked",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_protected_scope_blocked",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason="operator hint repair touched protected workflow",
    )


@pytest.mark.unit
async def test_operator_hint_repair_marks_diff_unavailable_push_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair could not compare protected scope",
        operation_id="op_protected_scope_diff_unavailable_hint",
        requested_at="2026-05-31T05:30:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_protected_scope_block(**_kwargs: object) -> None:
        return None

    async def _diff_unavailable_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="protected-scope diff unavailable blocked the operator hint repair push",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_protected_scope_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _diff_unavailable_push)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_diff_unavailable",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_diff_unavailable",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.terminal_monitor_failure is True
    assert result.protected_scope_diff_unavailable is True
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="needs_human",
        status_reason=("protected-scope diff unavailable blocked the operator hint repair push"),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason_code", "status_reason"),
    [
        (
            "PROTECTED_SCOPE_PUSH_BLOCKED",
            "operator hint repair touched protected workflow",
        ),
        (
            "PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            "protected-scope policy could not verify the operator hint repair push",
        ),
    ],
)
async def test_operator_hint_terminal_failure_persists_needs_human_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    status_reason: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair would edit a protected workflow",
        operation_id="op_terminal_protected_scope_hint",
        requested_at="2026-05-31T02:40:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()

    state = MonitorState(pending_operator_hint=hint)

    async def _terminal_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        operator_hints.mark_operator_hint_needs_human(state_arg, status_reason)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=status_reason,
            reason_code=reason_code,
        )

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _terminal_operator_hint_cycle)

    terminal = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    assert persisted.status == WorkspaceStatus.failed.value
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint == {
        "operation_id": "op_terminal_protected_scope_hint",
        "reason": "operator hint repair would edit a protected workflow",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": "2026-05-31T02:40:00+00:00",
        "status": "needs_human",
        "status_reason": status_reason,
    }


@pytest.mark.unit
async def test_operator_hint_pushed_processed_status_is_persisted_before_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair produced a fix commit",
        operation_id="op_pushed_processed_persisted",
        requested_at="2026-05-31T05:20:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()

    state = MonitorState(pending_operator_hint=hint)

    async def _pushed_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        operator_hints.mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _pushed_operator_hint_cycle)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
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
    monitor_state = dict(persisted.monitor_threads_addressed)
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert monitor_state[operator_hint_processed_key("op_pushed_processed_persisted")] == (
        "processed"
    )
    assert operation.result["outcome"] == "operator_hint_pushed"
    assert operation.result["pushed"] is True


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", ["needs_human", "agent_failed"])
async def test_operator_hint_non_pushed_terminal_status_is_persisted_before_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: Literal["needs_human", "agent_failed"],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair cannot produce a safe fix commit",
        operation_id=f"op_non_pushed_{terminal_status}",
        requested_at="2026-05-31T03:20:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()

    state = MonitorState(pending_operator_hint=hint)
    status_reason = f"operator hint ended as {terminal_status}"

    async def _non_pushed_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        if terminal_status == "needs_human":
            operator_hints.mark_operator_hint_needs_human(state_arg, status_reason)
        else:
            operator_hints.mark_operator_hint_agent_failed(state_arg, status_reason)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _non_pushed_operator_hint_cycle)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
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
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint == {
        "operation_id": f"op_non_pushed_{terminal_status}",
        "reason": "operator hint repair cannot produce a safe fix commit",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": "2026-05-31T03:20:00+00:00",
        "status": terminal_status,
        "status_reason": status_reason,
    }
    expected_outcome = (
        "operator_hint_needs_human"
        if terminal_status == "needs_human"
        else "operator_hint_agent_failed"
    )
    assert operation.result["outcome"] == expected_outcome


@pytest.mark.unit
async def test_operator_hint_noop_processed_status_is_persisted_before_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint only required review-thread bookkeeping",
        operation_id="op_noop_processed_persisted",
        requested_at="2026-05-31T04:55:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()

    state = MonitorState(pending_operator_hint=hint)

    async def _processed_noop_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        operator_hints.mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _processed_noop_operator_hint_cycle)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
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
    monitor_state = dict(persisted.monitor_threads_addressed)
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert monitor_state[operator_hint_processed_key("op_noop_processed_persisted")] == "processed"
    assert operation.result["outcome"] == "operator_hint_processed"
    assert operation.result["pushed"] is False


@pytest.mark.unit
async def test_operator_hint_repair_records_agent_failed_verdict_as_agent_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="operator hint repair should preserve agent failures",
        operation_id="op_agent_failed_hint",
        requested_at="2026-05-31T01:10:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _agent_failed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="agent_failed", reason="adapter crashed")

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _agent_failed)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_agent_failed",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_agent_failed",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result == _GitPushResult(pushed=False, failed=False, returncode=0)
    assert state.pending_operator_hint == OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        status="agent_failed",
        status_reason="adapter crashed",
    )


@pytest.mark.unit
async def test_operator_hint_repair_marks_successful_noop_push_as_processed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="reply to the relevant unresolved review thread without code changes",
        operation_id="op_noop_processed_hint",
        requested_at="2026-05-31T04:40:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    validated_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_without_commit(**_kwargs: object) -> VerdictResult:
        return VerdictResult(
            verdict="fix_committed",
            reason="operator hint handled without a code change",
        )

    async def _no_protected_scope_block(**_kwargs: object) -> None:
        return None

    async def _validated_noop_push(**kwargs: object) -> _GitPushResult:
        validated_calls.append(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=False,
            returncode=0,
            stderr="Everything up-to-date",
        )

    async def _unexpected_head_lookup(_worktree_path: Path) -> str:
        pytest.fail("no-op operator hint repairs should not refresh pushed HEAD")

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _start_head_ok,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_without_commit)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_protected_scope_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_noop_push)
    monkeypatch.setattr(runner, "_rev_parse_head", _unexpected_head_lookup)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_noop_processed",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_noop_processed",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    assert validated_calls
    assert state.pending_operator_hint is None
    assert (
        state.threads_addressed_ids[operator_hint_processed_key("op_noop_processed_hint")]
        == "processed"
    )
    assert state.last_push_sha is None


@pytest.mark.unit
async def test_operator_hint_repair_uses_captured_operation_start_head_for_protected_scope(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="repair must roll back only the current operation delta",
        operation_id="op_leftover_worktree",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _leftover_worktree_start_head(**_kwargs: object) -> tuple[str, None]:
        return ("leftover-worktree-head", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _protected_scope_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        )

    async def _capture_protected_scope_repair(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _pushed_head(_worktree_path: Path) -> str:
        return "pushed-head"

    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        _no_preexisting_dirty,
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        _leftover_worktree_start_head,
    )
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_scope_block)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_commits_before_push",
        _capture_protected_scope_repair,
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _pushed_head)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_leftover_head",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="pr-head-sha",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_leftover_head",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert captured["operation_start_head"] == "leftover-worktree-head"
    assert captured["source_head_sha"] == "leftover-worktree-head"
    assert state.pending_operator_hint is None
    assert state.last_push_sha == "pushed-head"


@pytest.mark.unit
async def test_monitor_state_round_trips_pending_operator_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    hint = OperatorHint(
        reason="fix the stale docs CTA",
        operation_id="op_hint_roundtrip",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    mark_operator_hint_processed(state)
    await runner._persist_state(workspace_id, state)

    assert state.pending_operator_hint is None
    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    assert OPERATOR_HINT_STATE_KEY not in persisted.monitor_threads_addressed
    assert (
        persisted.monitor_threads_addressed[operator_hint_processed_key("op_hint_roundtrip")]
        == "processed"
    )


@pytest.mark.unit
async def test_load_state_ignores_processed_pending_operator_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    hint = OperatorHint(
        reason="fix the stale docs CTA",
        operation_id="op_hint_processed_before_load",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    processed_key = operator_hint_processed_key("op_hint_processed_before_load")
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            {processed_key: "processed", "review-thread": "fix_committed"},
            hint,
        )
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)
    action = decide(_ready_status(), state, MonitorConfig(auto_merge=True))

    assert state.pending_operator_hint is None
    assert OPERATOR_HINT_STATE_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[processed_key] == "processed"
    assert state.threads_addressed_ids["review-thread"] == "fix_committed"
    assert isinstance(action, Merge)


@pytest.mark.unit
async def test_persist_state_preserves_concurrent_processed_operator_hint_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    hint = OperatorHint(
        reason="fix the stale docs CTA",
        operation_id="op_hint_processed_elsewhere",
        requested_at="2026-05-30T12:00:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            {"review-thread": "fix_committed"},
            hint,
        )
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == hint

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {
            "review-thread": "fix_committed",
            operator_hint_processed_key("op_hint_processed_elsewhere"): "processed",
        }
        await session.commit()

    stale_state.mark_addressed("second-thread", "fix_committed")
    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = persisted.monitor_threads_addressed
    assert stale_state.pending_operator_hint is None
    assert (
        stale_state.threads_addressed_ids[
            operator_hint_processed_key("op_hint_processed_elsewhere")
        ]
        == "processed"
    )
    assert OPERATOR_HINT_STATE_KEY not in monitor_state
    assert monitor_state[operator_hint_processed_key("op_hint_processed_elsewhere")] == "processed"
    assert monitor_state["review-thread"] == "fix_committed"
    assert monitor_state["second-thread"] == "fix_committed"


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", ["needs_human", "agent_failed"])
async def test_persist_state_preserves_concurrent_terminal_operator_hint_status(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_status: Literal["needs_human", "agent_failed"],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    pending_hint = OperatorHint(
        reason="investigate the operator supplied remonitor hint",
        operation_id="op_hint_terminal_elsewhere",
        requested_at="2026-05-31T00:30:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, pending_hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == pending_hint

    terminal_reason = "agent already determined this hint requires human attention"
    terminal_hint = OperatorHint(
        reason=pending_hint.reason,
        operation_id=pending_hint.operation_id,
        requested_at=pending_hint.requested_at,
        status=terminal_status,
        status_reason=terminal_reason,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, terminal_hint)
        await session.commit()

    stale_state.mark_addressed("second-thread", "fix_committed")
    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint["operation_id"] == "op_hint_terminal_elsewhere"
    assert persisted_hint["status"] == terminal_status
    assert persisted_hint["status_reason"] == terminal_reason
    assert monitor_state["second-thread"] == "fix_committed"


@pytest.mark.unit
@pytest.mark.parametrize("terminal_status", ["needs_human", "agent_failed"])
async def test_refresh_operator_state_imports_concurrent_terminal_same_operation_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    terminal_status: Literal["needs_human", "agent_failed"],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    pending_hint = OperatorHint(
        reason="investigate the operator supplied remonitor hint",
        operation_id="op_hint_refresh_terminal_elsewhere",
        requested_at="2026-05-31T00:45:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, pending_hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == pending_hint
    assert await runner._refresh_operator_state_from_workspace(workspace_id, stale_state) is False
    assert stale_state.pending_operator_hint == pending_hint

    terminal_reason = "another monitor pass could not safely apply the hint"
    terminal_hint = OperatorHint(
        reason=pending_hint.reason,
        operation_id=pending_hint.operation_id,
        requested_at=pending_hint.requested_at,
        status=terminal_status,
        status_reason=terminal_reason,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, terminal_hint)
        await session.commit()

    changed = await runner._refresh_operator_state_from_workspace(workspace_id, stale_state)
    action = decide(_ready_status(), stale_state, MonitorConfig(auto_merge=True))

    assert changed is True
    assert stale_state.pending_operator_hint == terminal_hint
    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_refresh_operator_state_clears_processed_operator_hint_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    hint = OperatorHint(
        reason="operator hint was processed by another monitor pass",
        operation_id="op_hint_refresh_processed_elsewhere",
        requested_at="2026-05-31T01:05:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint({}, hint)
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    stale_workspace = await runner._load_workspace(workspace_id)
    stale_state = runner._load_state(stale_workspace)
    assert stale_state.pending_operator_hint == hint

    processed_key = operator_hint_processed_key("op_hint_refresh_processed_elsewhere")
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {processed_key: "processed"}
        await session.commit()

    changed = await runner._refresh_operator_state_from_workspace(workspace_id, stale_state)
    action = decide(_ready_status(), stale_state, MonitorConfig(auto_merge=True))

    assert changed is True
    assert stale_state.pending_operator_hint is None
    assert stale_state.threads_addressed_ids[processed_key] == "processed"
    assert isinstance(action, Merge)


async def _seed_active_grant(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    path: str,
    block_epoch: int = 0,
) -> str:
    from awf.common.ids import new_operator_grant_id
    from awf.db.models import OperatorGrantAuditRecord

    grant_id = new_operator_grant_id()
    async with factory() as session:
        session.add(
            OperatorGrantAuditRecord(
                id=grant_id,
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved the protected change",
                normalized_path=path,
                block_epoch=block_epoch,
                approve_policy_downgrade=True,
            )
        )
        await session.commit()
    return grant_id


@pytest.mark.unit
async def test_operator_hint_grant_only_resume_skips_cli_and_consumes_grant(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only (approve-and-keep) resume — no directive but an active grant —
    skips the CLI and pushes the preserved commit through the grant-aware gate,
    then consumes the grant (single-use)."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_grant_only",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None  # hint processed
    async with factory() as session:
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is not None  # single-use


async def _set_block_resume_phase(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, resume_phase: str
) -> None:
    """Record a protected-scope block ``resume_phase`` on the workspace row."""
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_resume_phase = resume_phase
        await session.commit()


@pytest.mark.unit
async def test_operator_hint_resume_uses_generic_validator_off_sync_base(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary remonitor / non-sync-base protected-block resume must NOT thread
    ``base_branch`` into ``_protected_scope_push_block``. The monitor loop always
    supplies ``base_branch``, but the sync-base validator drops paths whose final tree
    matches the merged base — so a repair that reverts an unowned protected file back
    to base contents would bypass the gate. Gate on the recorded resume phase: a
    ``monitor_protected_scope_push`` (or absent) phase falls back to the generic
    unpushed-commit validator (``base_branch is None``) (PRRT_kwDOSJAM6s6KFZN_)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(factory, workspace_id, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_generic_validator",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    captured: dict[str, object] = {}

    async def _capture_block(**kwargs: object) -> None:
        captured.update(kwargs)

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _capture_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert captured["base_branch"] is None


@pytest.mark.unit
async def test_operator_hint_resume_threads_base_branch_into_protected_scope_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume of a sync-base-originated block (``monitor_protected_scope_sync_base``)
    must thread ``base_branch`` into ``_protected_scope_push_block`` so it re-validates
    with the sync-base-aware validator that filters out base-owned protected changes
    — matching how ``_run_sync_base`` first raised the block. Omitting it would run
    the generic validator and re-block on a target-branch-owned change a directive
    cannot revert (PRRT_kwDOSJAM6s6KFDHO)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_block_resume_phase(
        factory, workspace_id, MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_base_branch_thread",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    captured: dict[str, object] = {}

    async def _capture_block(**kwargs: object) -> None:
        captured.update(kwargs)

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _capture_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert captured["base_branch"] == "main"


@pytest.mark.unit
async def test_operator_hint_resume_reblocks_when_violation_unresolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive resume that does NOT clear the protected violation RE-BLOCKS
    (routes to the pause) instead of proceeding toward merge."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_reblock",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    still_blocking = _ProtectedScopePushBlock(
        message="still touches a protected file",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        ),
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _still_blocking(**_kwargs: object) -> _ProtectedScopePushBlock:
        return still_blocking

    captured: dict[str, object] = {}

    async def _pause(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a re-block must NOT push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _still_blocking)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert captured["protected_scope_block"] is still_blocking
    # A landed re-block must clear the in-memory monitor hint so the state the
    # loop persists afterward does not show a pending resume while the workspace
    # is already ``blocked`` (the bumped block epoch supersedes this hint).
    assert state.pending_operator_hint is None


@pytest.mark.unit
@pytest.mark.parametrize("paused", [True, False])
async def test_operator_hint_reblock_clears_hint_only_when_pause_lands(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    """The re-block clears the pending monitor hint only when the pause actually
    transitioned the workspace into ``blocked``. A stale CAS (the row already left
    ``monitoring_pr``) returns a plain failed result and PRESERVES the hint so the
    caller's normal failed handling runs against whatever terminal state won."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the protected edit",
        directive="revert it",
        operation_id="op_reblock_branch",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    still_blocking = _ProtectedScopePushBlock(
        message="still touches a protected file",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(path="pyproject.toml", protected_pattern="pyproject.toml"),
        ),
    )

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _still_blocking(**_kwargs: object) -> _ProtectedScopePushBlock:
        return still_blocking

    async def _pause(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=paused,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _still_blocking)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is paused
    if paused:
        assert state.pending_operator_hint is None
    else:
        assert state.pending_operator_hint == hint


@pytest.mark.unit
async def test_operator_hint_resume_no_op_push_when_commit_already_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Divergence recovery: a restart that finds the preserved commit already on
    the remote treats the push as a no-op (no duplicate push)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_idempotent",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _already_on_remote(**_kwargs: object) -> bool:
        return True

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed preserved commit must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    assert state.last_push_sha == "preserved-sha"
    assert state.pending_operator_hint is None  # hint processed


@pytest.mark.unit
async def test_operator_hint_resume_no_op_push_with_missing_preserved_commit_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only resume from a worktree reset/recreated at the remote head pushes
    a no-op (everything up-to-date) — but the recorded preserved commit never landed.
    Consuming the grant and marking the hint processed here would silently drop the
    approved protected change (PRRT_kwDOSJAM6s6KEtU2). The recorded preserved SHA not
    being on the remote must keep the grant active and surface needs_human."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_dropped_preserved",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _no_op_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "reset-to-remote-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _no_op_push)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    # The hint is NOT processed — it is surfaced for human attention instead.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    # The grant is NOT consumed: the approved protected change never landed.
    async with factory() as session:
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is None


@pytest.mark.unit
async def test_operator_hint_resume_threads_recorded_preserved_sha_into_no_op_check(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant-only resume must thread the pause-recorded preserved HEAD SHA
    into the idempotent no-op check so a reset/recreated worktree cannot make it
    silently drop the approved protected commit (PRRT_kwDOSJAM6s6KEHsN)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_thread",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _already_on_remote(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "recorded-preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert captured.get("preserved_head_sha") == "recorded-preserved-sha"


@pytest.mark.unit
async def test_operator_hint_grant_consumed_restart_skips_cli_when_commit_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart-after-consume recovery (PRRT_kwDOSJAM6s6KELkL): a grant-only resume
    can push the preserved commit, consume its grant (committed immediately), then
    crash before the processed marker persists. On restart the grant is gone, so a
    no-directive hint would otherwise re-run the CLI on just the reason string. The
    durable preserved-head marker plus the approved commit already being on the
    remote must short-circuit to bookkeeping WITHOUT invoking the agent."""
    workspace_id = await seed_monitoring_workspace(factory)
    # No active grant: it was already consumed by the crashed prior pass.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_grant_consumed_restart",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    captured: dict[str, object] = {}

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a consumed-grant restart must NOT re-invoke the CLI")

    async def _already_on_remote(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an already-pushed preserved commit must NOT be re-pushed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "recorded-preserved-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _already_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is False
    assert result.failed is False
    # The recorded preserved SHA is threaded into the positive-confirmation check.
    assert captured.get("preserved_head_sha") == "recorded-preserved-sha"
    assert state.last_push_sha == "recorded-preserved-sha"
    assert state.pending_operator_hint is None  # hint processed without the agent
    # The preserved-head marker MUST be cleared once the resume is finalized, so a
    # later plain remonitor cannot take this restart shortcut on a stale marker
    # (PRRT_kwDOSJAM6s6KE2BX).
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_operator_hint_resume_push_clears_preserved_head_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a grant-only resume pushes the approved commit and is finalized, the
    preserved-head marker MUST be dropped from monitor state. Leaving it would let
    a later plain remonitor (no directive, no grant) whose old preserved commit is
    still on the remote take the restart-recovery shortcut and silently skip the
    CLI, ignoring the operator's new repair request (PRRT_kwDOSJAM6s6KE2BX)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_active_grant(factory, workspace_id, path="pyproject.toml")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="approved the protected change",
        operation_id="op_clears_marker",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert state.pending_operator_hint is None  # hint processed
    # The marker is gone, so a subsequent plain remonitor will run the CLI.
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids


@pytest.mark.unit
async def test_operator_hint_no_directive_no_grant_no_marker_still_runs_cli(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain remonitor hint (no directive, no grant, NO preserved-head marker)
    must still invoke the CLI — the restart short-circuit only applies to a
    protected-block grant resume, never to a normal remonitor."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="re-examine the PR",
        operation_id="op_plain_remonitor",
        requested_at="2026-06-17T00:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    cli_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**kwargs: object) -> VerdictResult:
        cli_calls.append(kwargs)
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)

    result = await runner._run_operator_hint_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    assert cli_calls  # the CLI ran for the plain remonitor
    assert state.pending_operator_hint is None


@pytest.mark.unit
def test_monitor_while_blocked_new_comment_not_dropped_on_resume() -> None:
    """Scope #3: the reserved protected-block state keys (preserved-head marker,
    epoch/content notification key) must NOT mark an untriaged comment addressed.
    A review comment that arrived during the block yields ``AddressComments`` once
    the operator hint has been processed on resume — it is not silently dropped."""
    new_thread = ReviewThread(
        thread_id="T_during_block",
        path="src/awf/x.py",
        line=1,
        body_excerpt="please tweak this",
        author="reviewer",
    )
    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(new_thread,),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=(),
    )
    # State carrying the protected-block reserved keys, with the operator hint
    # already processed (cleared) — the resume's next decide() cycle.
    state = MonitorState(
        threads_addressed_ids={
            "__awf_protected_block_preserved_head__": "preserved-sha",
            "__awf_protected_block__:1:digestA": "notified",
        },
        pending_operator_hint=None,
    )

    action = decide(status, state, MonitorConfig(auto_merge=True))

    assert isinstance(action, AddressComments)
    assert new_thread in action.threads


@pytest.mark.unit
async def test_address_comments_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-cycle that pauses into ``blocked`` ends the monitor cycle cleanly:
    the loop returns True (stop) and records a ``protected_scope_paused`` outcome
    rather than terminally failing the workspace."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    thread = ReviewThread(
        thread_id="T_paused",
        path="src/awf/x.py",
        line=1,
        body_excerpt="tweak",
        author="reviewer",
    )
    state = MonitorState()

    async def _paused_fix_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_fix_cycle", _paused_fix_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
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
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
async def test_operator_hint_paused_into_blocked_ends_monitor_without_failing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-hint resume that re-pauses into ``blocked`` ends the monitor
    cycle cleanly with a ``protected_scope_paused`` outcome (not a failure)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert",
        directive="revert it",
        operation_id="op_paused",
        requested_at="2026-06-16T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _paused_hint_cycle(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PAUSED_BLOCKED",
            paused_into_blocked=True,
        )

    async def _terminate_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a paused workspace must NOT be terminally failed")

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _paused_hint_cycle)
    monkeypatch.setattr(runner, "_terminate_failed", _terminate_must_not_run)

    handled = await runner._execute(
        action=AddressOperatorHint(hint=hint),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert handled is True
    async with factory() as session:
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
    assert operation.result["outcome"] == "protected_scope_paused"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changed_paths", "expected"),
    [((), True), (("src/awf/x.py",), False)],
)
async def test_preserved_commit_already_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_paths: tuple[str, ...],
    expected: bool,
) -> None:
    """An empty changed-path set vs the remote PR branch means the preserved
    commit is already pushed (no-op); a non-empty set means there is work to push."""
    worktree = tmp_path / "worktrees" / "ws_div"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", changed_paths)

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    result = await runner._preserved_commit_already_on_remote(
        workspace_id="ws_div",
        worktree_path=worktree,
        remote_branch="awf/ws_div",
    )
    assert result is expected


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_missing_worktree_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            remote_branch="awf/ws_missing",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_diff_error_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktrees" / "ws_err"
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise ProtectedScopeDiffError("fetch failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_err",
            worktree_path=worktree,
            remote_branch="awf/ws_err",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_not_on_remote_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree reset/recreated at the remote head during the blocked interval:
    the diff is empty but the recorded preserved SHA is NOT on the branch, so the
    no-op MUST be refused or the approved protected commit is silently dropped."""
    worktree = tmp_path / "worktrees" / "ws_reset"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    # merge-base --is-ancestor <preserved> FETCH_HEAD exits non-zero: not on remote.
    cmd.queue_result(returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_reset",
            worktree_path=worktree,
            remote_branch="awf/ws_reset",
            preserved_head_sha="preserved-sha",
        )
        is False
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert ancestry_calls, "expected a merge-base --is-ancestor verification"
    assert "preserved-sha" in ancestry_calls[0].args
    assert "FETCH_HEAD" in ancestry_calls[0].args


@pytest.mark.unit
async def test_preserved_commit_already_on_remote_recorded_sha_on_remote_returns_true(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine idempotent restart: the preserved commit truly landed on the
    remote (ancestry check passes), so the push is a legitimate no-op."""
    worktree = tmp_path / "worktrees" / "ws_idem"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("base-sha", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_already_on_remote(
            workspace_id="ws_idem",
            worktree_path=worktree,
            remote_branch="awf/ws_idem",
            preserved_head_sha="preserved-sha",
        )
        is True
    )


@pytest.mark.unit
async def test_protected_block_persists_preserved_head_marker_atomically(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserved HEAD SHA must be durable on the workspace row AS SOON AS the
    ``monitoring_pr -> blocked`` transition commits — not only in the in-memory
    ``state`` that the loop flushes later. A crash after the block commit but
    before ``_persist_state`` would otherwise lose the only monitor-state copy of
    the preserved head; the next grant-only resume would read
    ``preserved_head_sha=None`` and treat a reset/recreated worktree's empty diff
    as already-pushed, silently dropping the approved commit (PRRT_kwDOSJAM6s6KEtU6)."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "blocked-head-sha"

    async def _no_notification(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _head)
    monkeypatch.setattr(runner, "_post_protected_block_notification", _no_notification)

    # A fresh state whose in-memory marker we deliberately IGNORE afterwards: the
    # durable copy must come from the block commit, not from a later flush.
    state = MonitorState()
    block = _ProtectedScopePushBlock(
        message="protected scope blocked",
        reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
        violations=(
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            ),
        ),
    )

    result = await runner._pause_monitor_for_protected_scope_block(
        workspace_id=workspace_id,
        pr_number=42,
        pr_head_sha="start-sha",
        protected_scope_block=block,
        worktree_path=worktree,
        state=state,
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.paused_into_blocked is True
    # The marker is durable on the row WITHOUT any later ``_persist_state`` flush.
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert (workspace.monitor_threads_addressed or {}).get(
            _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY
        ) == "blocked-head-sha"
