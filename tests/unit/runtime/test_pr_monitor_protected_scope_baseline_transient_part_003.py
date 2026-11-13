"""#1221: a baseline-park resume must prove its kept commit landed (PRRT_kwDOSJAM6s63M6sD).

The park records its preserved HEAD only in the transition event payload (no
monitor-state marker), so the resume reads it from there and requires the kept
commit on the remote before treating an empty worktree-to-remote diff as published.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from awf.runtime.operator_hints import OperatorHint
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime.test_pr_monitor_protected_scope_baseline_transient import (
    _REPAIR_COMMIT_SHA,
    _runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _park(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    cmd: FakeCommandRunner,
) -> None:
    runner = _runner(factory, tmp_path, cmd)
    result = await runner._park_for_protected_scope_baseline_unavailable(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktrees" / workspace_id,
        remote_branch=f"awf/{workspace_id}",
        resume_phase=MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
        message="baseline unavailable",
    )
    assert result.paused_into_blocked is True
    # ``guide`` resumes the parked workspace back into ``monitoring_pr``.
    async with factory() as s:
        await s.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(status=WorkspaceStatus.monitoring_pr.value)
        )
        await s.commit()


@pytest.mark.unit
async def test_baseline_park_preserved_head_reads_latest_park_transition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    assert await runner._protected_scope_baseline_park_preserved_head(workspace_id) is None

    recorded = FakeCommandRunner()
    recorded.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")
    await _park(factory, tmp_path, workspace_id, recorded)
    assert await runner._protected_scope_baseline_park_preserved_head(workspace_id) == (
        _REPAIR_COMMIT_SHA
    )

    # A later park whose HEAD was unresolvable supersedes the older SHA.
    unresolved = FakeCommandRunner()
    unresolved.queue_result(returncode=128, stderr="fatal: not a git repository")
    await _park(factory, tmp_path, workspace_id, unresolved)
    assert await runner._protected_scope_baseline_park_preserved_head(workspace_id) is None


@pytest.mark.unit
async def test_baseline_park_resume_no_op_requires_kept_commit_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree recreated at the remote head must not finalize the park as published."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")
    await _park(factory, tmp_path, workspace_id, cmd)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_recreated",
        requested_at="2026-11-13T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    containment_checks: list[str | None] = []

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _empty_diff_kept_commit_missing(
        *, preserved_head_sha: str | None = None, **_kwargs: object
    ) -> bool:
        # The worktree matches the remote head, but the kept commit is not on it.
        containment_checks.append(preserved_head_sha)
        return preserved_head_sha is None

    async def _up_to_date(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(
        runner, "_preserved_commit_already_on_remote", _empty_diff_kept_commit_missing
    )
    monkeypatch.setattr(runner, "_validated_git_push_result", _up_to_date)

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

    # Both the idempotent no-op and the no-op-push fall-through require the kept SHA.
    assert containment_checks == [_REPAIR_COMMIT_SHA, _REPAIR_COMMIT_SHA]
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert _REPAIR_COMMIT_SHA in (state.pending_operator_hint.status_reason or "")
    # Not finalized: the park stays unsettled for the operator.
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True


@pytest.mark.unit
async def test_baseline_park_resume_no_op_accepts_the_kept_commits_baseline_rewrite(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A park HEAD the resume reset to the published fix-pass baseline is not lost.

    Keeping the fix-pass baseline discards the parked SHA by design; the no-op guard
    must require the commit that replaced it, or a directiveless resume would park
    needs_human instead of finalizing the empty repair (PRRT_kwDOSJAM6s63OA3D).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")
    await _park(factory, tmp_path, workspace_id, cmd)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_rewritten",
        requested_at="2026-11-13T10:00:00+00:00",
    )
    baseline_sha = "e" * 40
    state = MonitorState(pending_operator_hint=hint)
    state.threads_addressed_ids[PROVIDER_PARK_RESUME_REWRITTEN_HEAD_STATE_KEY] = json.dumps(
        {"from": _REPAIR_COMMIT_SHA, "to": baseline_sha}
    )
    containment_checks: list[str | None] = []

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _baseline_on_remote(
        *, preserved_head_sha: str | None = None, **_kwargs: object
    ) -> bool:
        containment_checks.append(preserved_head_sha)
        return preserved_head_sha == baseline_sha

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _baseline_on_remote)

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

    assert containment_checks == [baseline_sha]
    assert result.failed is False
    # Finalized: the hint is retired rather than parked needs_human.
    assert state.pending_operator_hint is None
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is False


@pytest.mark.unit
async def test_baseline_park_resume_no_op_refuses_without_a_recorded_kept_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A park whose HEAD was unresolvable must not finalize on an empty diff alone.

    With no kept SHA the remote check has nothing to confirm, so an empty
    worktree-to-remote diff (a worktree recreated at the remote head) would
    finalize the park without proof the repair landed (PRRT_kwDOSJAM6s63OKfO).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    await _park(factory, tmp_path, workspace_id, cmd)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_unrecorded",
        requested_at="2026-11-13T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _empty_diff(*, preserved_head_sha: str | None = None, **_kwargs: object) -> bool:
        assert preserved_head_sha is None
        return True

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _empty_diff)

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
    assert result.returncode == 1
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "no preserved HEAD" in (state.pending_operator_hint.status_reason or "")
    # Not finalized: the park stays unsettled for the operator.
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True


@pytest.mark.unit
async def test_baseline_park_resume_no_op_push_refuses_without_a_recorded_kept_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op-push fall-through must not finalize a park with no kept SHA either.

    When the early empty-diff check does not fire (a diff/fetch failure or a
    non-empty diff) and the push then no-ops, there is still no recorded commit to
    prove the kept repair landed (PRRT_kwDOSJAM6s63O2cy).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    await _park(factory, tmp_path, workspace_id, cmd)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_unrecorded_push",
        requested_at="2026-11-13T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _diff_unavailable(**_kwargs: object) -> bool:
        return False

    async def _up_to_date(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _diff_unavailable)
    monkeypatch.setattr(runner, "_validated_git_push_result", _up_to_date)

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
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "no preserved HEAD" in (state.pending_operator_hint.status_reason or "")
    # Not finalized: the park stays unsettled for the operator.
    assert await runner._protected_scope_baseline_park_resume_pending(workspace_id) is True


async def _run_pushed_baseline_park_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kept_commit_on_remote_after_push: bool,
    park_head_resolves: bool = True,
) -> tuple[object, MonitorState, list[str | None], _GitPushResult]:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    if park_head_resolves:
        cmd.queue_result(returncode=0, stdout=f"{_REPAIR_COMMIT_SHA}\n")
    else:
        cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    await _park(factory, tmp_path, workspace_id, cmd)
    runner = _runner(factory, tmp_path, FakeCommandRunner())
    hint = OperatorHint(
        reason="resume after baseline outage",
        operation_id="op_resume_pushed",
        requested_at="2026-11-13T10:00:00+00:00",
    )
    state = MonitorState(pending_operator_hint=hint)
    containment_checks: list[str | None] = []
    new_head_sha = "d" * 40

    async def _none(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fix_committed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _kept_commit_check(
        *, preserved_head_sha: str | None = None, **_kwargs: object
    ) -> bool:
        containment_checks.append(preserved_head_sha)
        # Pre-push: the agent's new commit is unpushed, so this is no idempotent no-op.
        # Post-push: the remote now matches HEAD; only containment decides.
        return len(containment_checks) > 1 and kept_commit_on_remote_after_push

    async def _pushed(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(_worktree_path: Path) -> str:
        return new_head_sha

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _none)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fix_committed)
    monkeypatch.setattr(runner, "_post_action_pr_terminal_push_result_if_moot", _none)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _none)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _kept_commit_check)
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
    assert state.last_push_sha == new_head_sha
    pending = await runner._protected_scope_baseline_park_resume_pending(workspace_id)
    return pending, state, containment_checks, result


@pytest.mark.unit
async def test_baseline_park_resume_push_requires_kept_commit_in_pushed_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful push of a NEW commit is no proof the kept repair landed.

    A worktree recreated at the remote head still runs the directiveless repair agent,
    whose new commit pushes cleanly without the parked repair; finalizing would clear
    the park while the kept commit never landed (PRRT_kwDOSJAM6s63PY73).
    """
    pending, state, containment_checks, result = await _run_pushed_baseline_park_resume(
        factory, tmp_path, monkeypatch, kept_commit_on_remote_after_push=False
    )

    assert containment_checks == [_REPAIR_COMMIT_SHA, _REPAIR_COMMIT_SHA]
    assert result.pushed is True
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert _REPAIR_COMMIT_SHA in (state.pending_operator_hint.status_reason or "")
    # Not finalized: the park stays unsettled for the operator.
    assert pending is True


@pytest.mark.unit
async def test_baseline_park_resume_push_finalizes_when_kept_commit_landed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending, state, containment_checks, result = await _run_pushed_baseline_park_resume(
        factory, tmp_path, monkeypatch, kept_commit_on_remote_after_push=True
    )

    assert containment_checks == [_REPAIR_COMMIT_SHA, _REPAIR_COMMIT_SHA]
    assert result.pushed is True
    assert state.pending_operator_hint is None
    assert pending is False


@pytest.mark.unit
async def test_baseline_park_resume_push_refuses_without_a_recorded_kept_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pushed NEW commit cannot finalize a park whose HEAD was never recorded.

    With no kept SHA there is nothing to prove on the remote, so the successful push
    of the repair agent's commit must not clear the park (PRRT_kwDOSJAM6s63QAPz).
    """
    pending, state, containment_checks, result = await _run_pushed_baseline_park_resume(
        factory,
        tmp_path,
        monkeypatch,
        kept_commit_on_remote_after_push=True,
        park_head_resolves=False,
    )

    assert containment_checks == [None]
    assert result.pushed is True
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "no preserved HEAD" in (state.pending_operator_hint.status_reason or "")
    # Not finalized: the park stays unsettled for the operator.
    assert pending is True
