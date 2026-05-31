"""Regression tests for operator remonitor hints in the PR monitor runner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
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
    decide,
)
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
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
async def test_persist_state_preserves_concurrent_operator_hint_and_freeze(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_last_commit_sha = head_sha
        workspace.monitor_threads_addressed = {
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
            "review-thread": "fix_committed",
        }
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
    assert stale_state.pending_operator_hint is None
    assert stale_state.threads_addressed_ids[initial_done_key] == "elapsed"
    assert stale_state.threads_addressed_ids[settle_done_key] == "elapsed"

    hint = OperatorHint(
        reason="do not merge until this operator warning is handled",
        operation_id="op_concurrent_hint",
        requested_at="2026-05-30T23:40:00+00:00",
    )
    freeze_now = datetime(2026, 5, 30, 23, 40, tzinfo=UTC)
    freeze_started_value = runner_helpers._initial_review_grace_wall_started_value_from_datetime(
        freeze_now
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = persist_operator_hint(dict(workspace.monitor_threads_addressed), hint)
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=freeze_now,
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    stale_state.mark_addressed("second-thread", "fix_committed")
    await runner._persist_state(workspace_id, stale_state)

    async with factory() as session:
        persisted = await WorkspaceRepository(session).get(workspace_id)

    assert persisted is not None
    monitor_state = dict(persisted.monitor_threads_addressed)
    persisted_hint = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert persisted_hint == {
        "operation_id": "op_concurrent_hint",
        "reason": "do not merge until this operator warning is handled",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": "2026-05-30T23:40:00+00:00",
        "status": "pending",
    }
    assert monitor_state[initial_started_key] == freeze_started_value
    assert monitor_state[settle_started_key] == freeze_started_value
    assert initial_done_key not in monitor_state
    assert settle_done_key not in monitor_state
    assert monitor_state["review-thread"] == "fix_committed"
    assert monitor_state["second-thread"] == "fix_committed"


@pytest.mark.unit
async def test_merge_rechecks_persisted_operator_hint_before_merge_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after the monitor loaded state",
        operation_id="op_merge_recheck",
        requested_at="2026-05-30T23:55:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    calls: list[OperatorHint] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert calls == [hint]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_recheck_preserves_remote_push_url_for_persisted_operator_hint(
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
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after a fork PR remote was selected",
        operation_id="op_merge_recheck_remote",
        requested_at="2026-05-31T00:25:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    remote_push_url = "https://github.com/fork-owner/aira-web.git"
    captured_remote_push_urls: list[str | None] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        captured_remote_push_urls.append(kwargs["remote_push_url"])
        state_arg = kwargs["state"]
        assert isinstance(state_arg, MonitorState)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=remote_push_url,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert captured_remote_push_urls == [remote_push_url]


@pytest.mark.unit
async def test_merge_recheck_dispatches_persisted_operator_hint_before_pre_merge_error(
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
        pre_merge_settle_seconds=2,
    )
    stale_state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived during the pre-merge settle window",
        operation_id="op_merge_recheck_error",
        requested_at="2026-05-31T00:10:00+00:00",
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = persist_operator_hint(
            dict(workspace.monitor_threads_addressed or {}),
            hint,
        )
        await session.commit()

    calls: list[OperatorHint] = []

    async def _raise_pre_merge_base_fetch_error(**_kwargs: object) -> PRStatus:
        raise BaseFetchError("base fetch failed while operator hint was pending")

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(
        runner,
        "_fetch_status_for_decision",
        _raise_pre_merge_base_fetch_error,
    )
    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert calls == [hint]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_merge_rechecks_freeze_only_remonitor_before_merge_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "f" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    stale_state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = dict(workspace.monitor_threads_addressed or {})
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=datetime.now(UTC),
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(head_sha=head_sha),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert OPERATOR_HINT_STATE_KEY not in stale_state.threads_addressed_ids
    assert initial_done_key not in stale_state.threads_addressed_ids
    assert settle_done_key not in stale_state.threads_addressed_ids
    assert initial_started_key in stale_state.threads_addressed_ids
    assert settle_started_key in stale_state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_final_recheck_blocks_hint_written_after_locked_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    state = MonitorState()
    hint = OperatorHint(
        reason="operator warning arrived after the locked merge gate",
        operation_id="op_final_merge_recheck",
        requested_at="2026-05-31T05:05:00+00:00",
    )
    original_merge_gate = runner._merge_gate_with_legacy_head_support
    merge_gate_calls = 0

    async def _write_hint_after_locked_gate(*args: object, **kwargs: object) -> object:
        nonlocal merge_gate_calls
        merge_gate_calls += 1
        result = await original_merge_gate(*args, **kwargs)
        if merge_gate_calls == 3:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                workspace.monitor_threads_addressed = persist_operator_hint(
                    dict(workspace.monitor_threads_addressed or {}),
                    hint,
                )
                await session.commit()
        return result

    calls: list[OperatorHint] = []

    async def _record_operator_hint_cycle(**kwargs: object) -> _GitPushResult:
        called_hint = kwargs["hint"]
        state_arg = kwargs["state"]
        assert isinstance(called_hint, OperatorHint)
        assert isinstance(state_arg, MonitorState)
        calls.append(called_hint)
        mark_operator_hint_processed(state_arg)
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(
        runner,
        "_merge_gate_with_legacy_head_support",
        _write_hint_after_locked_gate,
    )
    monkeypatch.setattr(runner, "_run_operator_hint_cycle", _record_operator_hint_cycle)

    terminal = await runner._execute(
        action=Merge(),
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

    assert terminal is False
    assert calls == [hint]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_final_recheck_waits_on_freeze_written_after_locked_gate(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head_sha = "d" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    settle_started_key = runner_helpers._non_check_reviewer_settle_started_key(
        pr_number=42,
        head_sha=head_sha,
    )
    state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )
    original_merge_gate = runner._merge_gate_with_legacy_head_support
    merge_gate_calls = 0

    async def _write_freeze_after_locked_gate(*args: object, **kwargs: object) -> object:
        nonlocal merge_gate_calls
        merge_gate_calls += 1
        result = await original_merge_gate(*args, **kwargs)
        if merge_gate_calls == 3:
            async with factory() as session:
                workspace = await WorkspaceRepository(session).get(workspace_id)
                assert workspace is not None
                monitor_state = dict(workspace.monitor_threads_addressed or {})
                operator_hints.arm_operator_hint_freeze(
                    monitor_state,
                    pr_number=42,
                    head_sha=head_sha,
                    now=datetime.now(UTC),
                )
                workspace.monitor_threads_addressed = monitor_state
                await session.commit()
        return result

    monkeypatch.setattr(
        runner,
        "_merge_gate_with_legacy_head_support",
        _write_freeze_after_locked_gate,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(head_sha=head_sha),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert settle_done_key not in state.threads_addressed_ids
    assert settle_started_key in state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_rechecks_initial_grace_after_visible_reviewer_freeze(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    head_sha = "e" * 40
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=42,
        head_sha=head_sha,
    )
    initial_done_key = runner_helpers._initial_review_grace_done_key(42)
    initial_started_key = runner_helpers._initial_review_grace_started_key(42)
    settle_done_key = runner_helpers._non_check_reviewer_settle_done_key(
        pr_number=42,
        head_sha=head_sha,
    )
    stale_state = MonitorState(
        threads_addressed_ids={
            initial_done_key: "elapsed",
            settle_done_key: "elapsed",
        }
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        monitor_state = dict(workspace.monitor_threads_addressed or {})
        operator_hints.arm_operator_hint_freeze(
            monitor_state,
            pr_number=42,
            head_sha=head_sha,
            now=datetime.now(UTC),
        )
        workspace.monitor_threads_addressed = monitor_state
        await session.commit()

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path,
        initial_review_grace_period_seconds=180,
        non_check_reviewer_settle_seconds=180,
        non_check_reviewer_logins=("greptile-apps",),
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_ready_status(
            head_sha=head_sha,
            checks=(CheckTiming(name="greptile-apps", conclusion="SUCCESS"),),
        ),
        state=stale_state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert initial_done_key not in stale_state.threads_addressed_ids
    assert initial_started_key in stale_state.threads_addressed_ids
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
