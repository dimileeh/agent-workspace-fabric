"""Regression tests for operator remonitor hints — clean protected diff slice."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _record_workflow_block_violation(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_violations = [
            {
                "path": ".github/workflows/ci.yml",
                "protected_pattern": ".github/workflows/",
                "section": "jobs.python-coverage-shards",
                "reason": "QUALITY_GATE_POLICY_CHANGED",
            }
        ]
        await session.commit()


@pytest.mark.unit
async def test_operator_hint_directive_clean_current_diff_pushes_dropped_preserved_history(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive that dropped the preserved workflow commit must not re-block only
    because the old preserved marker still exists in monitor state.

    This models a monitor-origin protected block for ``.github/workflows/ci.yml``.
    The operator directive has already produced a current PR/worktree diff that no
    longer includes that protected path, and the preserved commit is no longer in
    the unpushed range. That proves the directive reset the preserved commit away.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _record_workflow_block_violation(factory, workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="drop the workflow mutation and keep the scoped product changes",
        directive="reset .github/workflows/ci.yml and preserve the product/test edits",
        operation_id="op_clean_workflow_diff",
        requested_at="2026-06-27T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "stale-workflow-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("stale-workflow-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_current_violation(**_kwargs: object) -> None:
        return None

    async def _current_diff_without_workflow(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return (
            "remote-base-sha",
            (
                "src/awf/runtime/pr_monitor_runner/operator_hints.py",
                "tests/unit/runtime/test_pr_monitor_operator_hints_part_008.py",
            ),
        )

    async def _preserved_history_not_unpushed(**_kwargs: object) -> bool:
        return False

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _pause_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("clean current protected diff must not re-block on stale preserved history")

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "safe-product-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_current_violation)
    monkeypatch.setattr(
        runner, "_remote_branch_diff_base_and_changed_paths", _current_diff_without_workflow
    )
    monkeypatch.setattr(
        runner, "_preserved_commit_in_unpushed_range", _preserved_history_not_unpushed
    )
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause_must_not_run)
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
    assert result.failed is False
    assert state.pending_operator_hint is None
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids
    assert state.last_push_sha == "safe-product-head-sha"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.block_epoch == 0


@pytest.mark.unit
async def test_operator_hint_directive_clean_current_diff_reblocks_unpushed_preserved_history(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean current protected-path diff is not enough to prove a directive is safe.

    Reverting a protected file ON TOP of the preserved offending commit also removes
    the protected path from the net diff, but pushing HEAD would still publish the
    ungranted preserved commit. The leak guard must run the unpushed-history check
    before treating the preserved marker as stale.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _record_workflow_block_violation(factory, workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert the workflow mutation",
        directive="revert .github/workflows/ci.yml",
        operation_id="op_clean_workflow_diff_revert_on_top",
        requested_at="2026-06-27T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "unpushed-workflow-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("unpushed-workflow-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_current_violation(**_kwargs: object) -> None:
        return None

    async def _current_diff_without_workflow(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return (
            "remote-base-sha",
            (
                "src/awf/runtime/pr_monitor_runner/operator_hints.py",
                "tests/unit/runtime/test_pr_monitor_operator_hints_part_008.py",
            ),
        )

    async def _preserved_history_still_unpushed(**_kwargs: object) -> bool:
        return True

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

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
        pytest.fail("clean net diff must not publish unpushed preserved protected history")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_current_violation)
    monkeypatch.setattr(
        runner, "_remote_branch_diff_base_and_changed_paths", _current_diff_without_workflow
    )
    monkeypatch.setattr(
        runner, "_preserved_commit_in_unpushed_range", _preserved_history_still_unpushed
    )
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
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

    assert result.pushed is False
    assert result.paused_into_blocked is True
    leak_block = captured["protected_scope_block"]
    assert isinstance(leak_block, _ProtectedScopePushBlock)
    assert [violation.path for violation in leak_block.violations] == [".github/workflows/ci.yml"]
    assert "unpushed-workflow-sha" in leak_block.message
    assert state.pending_operator_hint is None
