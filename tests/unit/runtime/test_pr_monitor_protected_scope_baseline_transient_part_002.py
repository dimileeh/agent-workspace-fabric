"""#1221: operator-hint resume of a transient protected-scope baseline park.

Continuation of ``test_pr_monitor_protected_scope_baseline_transient``: the
operator-hint resume keeps the park's original resume phase, clears the hint
instead of parking it needs_human, and keeps the parked repair commit when the
resume push is rejected non-fast-forward.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OperatorHint
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner import operator_hints as operator_hints_module
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeBaselineUnavailableError,
)
from awf.runtime.validation_worktree_types import ValidationWorktreeCheck
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_protected_scope_baseline_transient import _runner


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
def test_worktree_check_extra_details_merge_into_details() -> None:
    check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        extra_details={"transient_baseline_unavailable": True},
    )

    details = check.details()

    assert details["transient_baseline_unavailable"] is True
    assert details["reason_code"] == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "transient_baseline_unavailable" not in ValidationWorktreeCheck(clean=True).details()


@pytest.mark.unit
async def test_operator_hint_resume_park_keeps_original_phase_and_clears_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _sync_base_phase(_workspace_id: str) -> str:
        return MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE

    async def _transient_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="baseline unavailable",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            transient_baseline_unavailable=True,
        )

    async def _parked(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="parked",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            paused_into_blocked=True,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_resolve_block_resume_phase", _sync_base_phase)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _transient_block)
    monkeypatch.setattr(runner, "_repair_protected_scope_commits_before_push", _parked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_park",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_park",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        base_branch="development",
    )

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    # The park bumps the block epoch, superseding this hint (like a re-block).
    assert state.pending_operator_hint is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "block_phase",
    [MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE],
)
async def test_operator_hint_validated_push_requests_original_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    block_phase: str,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_phase",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(*_args: object, **_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _block_phase(_workspace_id: str) -> str:
        return block_phase

    async def _pushed(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(pushed=False, failed=True, returncode=1, stderr="stop")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_resolve_block_resume_phase", _block_phase)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _pushed)

    await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_phase",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_phase",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        base_branch="development",
    )

    assert captured["protected_scope_resume_phase"] == block_phase


@pytest.mark.unit
async def test_operator_hint_commit_sink_park_clears_hint_instead_of_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_sink",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _raise_baseline(**_kwargs: object) -> None:
        raise ProtectedScopeBaselineUnavailableError("dns")

    async def _parked(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="parked",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            paused_into_blocked=True,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _raise_baseline)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _parked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_sink_park",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_sink_park",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert state.pending_operator_hint is None


async def _mark_baseline_parked(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, *, resume_phase: str | None
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                block_reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
                block_resume_phase=resume_phase,
            )
        )
        await s.commit()


@pytest.mark.unit
async def test_baseline_park_resume_pending_tracks_unsettled_park_only(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path, FakeCommandRunner())

    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is False
    await _mark_baseline_parked(
        factory, workspace_id, resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    )
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True
    # A finalized resume clears the phase: the kept commit was published or dropped.
    await _mark_baseline_parked(factory, workspace_id, resume_phase=None)
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is False
    assert await runner._protected_scope_baseline_park_resume_pending("ws_missing") is False


@pytest.mark.unit
async def test_operator_hint_resume_of_baseline_park_keeps_commit_on_rejected_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grantless guide resuming a baseline park must not reset --hard its commit.

    The PR branch advanced while the workspace was blocked, so the push is rejected
    non-fast-forward. The default resync would fetch the remote and reset the kept
    repair away; the resume suppresses it and parks the hint for a human instead.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _mark_baseline_parked(
        factory, workspace_id, resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    )
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_rejected",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _rejected(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="! [rejected] HEAD -> awf/x (non-fast-forward)",
            reason_code="GIT_PUSH_REJECTED_NON_FAST_FORWARD",
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_validated_git_push_result", _rejected)

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

    assert captured["allow_resync_on_rejection"] is False
    assert result.failed is False
    assert result.returncode == 1
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "kept" in (state.pending_operator_hint.status_reason or "")
    # The park stays unsettled, so a follow-up guide keeps suppressing the reset.
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("block_phase", "expected_phase"),
    [
        (
            MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
            MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
        ),
        (MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE),
        (None, MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE),
    ],
)
async def test_operator_hint_stranded_sink_park_keeps_original_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    block_phase: str | None,
    expected_phase: str,
) -> None:
    # A resume whose stranded-edits commit sink still cannot fetch the baseline
    # re-parks; it must keep a sync-base origin so the next resume selects the
    # sync-base-aware validator (PRRT_kwDOSJAM6s622NyQ).
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_stranded",
        requested_at="2026-11-12T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    captured: dict[str, object] = {}

    async def _raise_baseline(*_args: object, **_kwargs: object) -> None:
        raise ProtectedScopeBaselineUnavailableError("dns")

    async def _not_moot(**_kwargs: object) -> None:
        return None

    async def _recorded_phase(_workspace_id: str) -> str | None:
        return block_phase

    async def _parked(**kwargs: object) -> _GitPushResult:
        captured.update(kwargs)
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="parked",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            paused_into_blocked=True,
        )

    monkeypatch.setattr(
        operator_hints_module, "commit_provider_park_stranded_edits", _raise_baseline
    )
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _not_moot)
    monkeypatch.setattr(runner, "_resolve_block_resume_phase", _recorded_phase)
    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_push_result", _parked)

    result = await runner._run_operator_hint_cycle(
        workspace_id="ws_operator_hint_stranded_park",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        hint=hint,
        state=state,
        remote_branch="awf/ws_operator_hint_stranded_park",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        base_branch="development",
    )

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == expected_phase
    assert state.pending_operator_hint is None
