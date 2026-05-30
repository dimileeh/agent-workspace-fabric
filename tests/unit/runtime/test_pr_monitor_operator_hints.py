"""Regression tests for operator remonitor hints in the PR monitor runner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationType
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
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
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


def _ready_status(*, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


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
    assert state.pending_operator_hint == hint


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
