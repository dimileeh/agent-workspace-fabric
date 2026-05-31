"""Regression tests for operator remonitor hints in the PR monitor runner."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

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
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner import helpers as runner_helpers
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import BaseFetchError, ProtectedScopeDiffError
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
    assert state.pending_operator_hint == hint


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
