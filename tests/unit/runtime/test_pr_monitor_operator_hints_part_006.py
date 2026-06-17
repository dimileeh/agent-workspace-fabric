"""Regression tests for operator remonitor hints — validation-fix-pass slice (part 6).

Split out of ``test_pr_monitor_operator_hints_part_003`` to keep that module under
the first-party line limit. These cases cover how a grant-only resume disables the
validation fix passes, how a plain remonitor keeps them, and how a moved preserved
HEAD parks the terminal directive+grant hint instead of reblocking.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
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


async def _seed_grant_and_block_violations(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    grant_path: str,
    violation_paths: tuple[str, ...],
) -> str:
    """Seed an active operator grant and record the block's protected violations.

    Mirrors the state a combined ``--directive ... --grant ...`` guide leaves: the
    grant covers ``grant_path`` and ``block_violations`` lists the protected paths
    that caused the block (i.e. the preserved commit's protected changes)."""
    from awf.common.ids import new_operator_grant_id
    from awf.db.models import OperatorGrantAuditRecord
    from awf.db.repositories import WorkspaceRepository

    grant_id = new_operator_grant_id()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        session.add(
            OperatorGrantAuditRecord(
                id=grant_id,
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved keeping the protected change",
                normalized_path=grant_path,
                block_epoch=workspace.block_epoch,
                approve_policy_downgrade=True,
            )
        )
        workspace.block_violations = [{"path": path} for path in violation_paths]
        await session.commit()
    return grant_id


@pytest.mark.unit
async def test_operator_hint_grant_only_resume_disables_validation_fix_passes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only (approve-and-keep) resume must push with the pre-push validation
    fix passes DISABLED. The grant is consumed only after the push, so a fix pass
    committing through ``_commit_dirty_worktree`` — which honors the still-active
    grant — could publish new edits to the granted protected path under an approval
    meant only for the preserved commit (PR #609 comment 4512881681)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="keep the protected edit",
        directive=None,
        operation_id="op_grant_only",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GRANT",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-granted-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push)
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
    assert push_calls, "the grant-only resume push must run"
    assert push_calls[0]["allow_validation_fix_passes"] is False


@pytest.mark.unit
async def test_operator_hint_remonitor_without_grant_keeps_validation_fix_passes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive resume with NO active grant keeps the fix passes enabled: there is
    no grant to ride on, so the normal validation-repair loop must stay available."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="fix the failing tests",
        directive="redo the change",
        operation_id="op_directive_no_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("start-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push)
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
    assert push_calls, "the directive resume push must run"
    assert push_calls[0]["allow_validation_fix_passes"] is True


@pytest.mark.unit
async def test_operator_hint_grant_only_validation_failure_parks_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant-only (approve-and-keep) resume whose pre-push validation FAILS must
    park the hint at ``needs_human`` instead of leaving it pending.

    With an active grant the CLI is skipped (no directive) AND the validation fix
    passes are disabled, so nothing in the worktree can change between iterations. A
    plain ``PRE_PUSH_VALIDATION_FAILED`` is non-terminal, so leaving the hint pending
    would re-run the identical grant-only resume every monitor cycle — re-failing the
    same validation unchanged until the outer loop eventually fails the workspace,
    never surfacing an operator-actionable pause. Surface ``needs_human`` with a
    NON-failed result so the loop parks the workspace at ``monitoring_pr``
    (PRRT_kwDOSJAM6s6KI221)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="keep the protected edit",
        directive=None,
        operation_id="op_grant_only_fail",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GRANT",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr="pytest failed",
            reason_code=_PRE_PUSH_VALIDATION_FAILED_REASON,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push)

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

    # NON-failed result so the loop parks at ``monitoring_pr`` (it does not
    # ``_terminate_failed`` and the grant survives for a re-resume after the
    # operator fixes the preserved commit or withdraws the approval).
    assert result.failed is False
    assert result.pushed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "pytest failed" in (state.pending_operator_hint.status_reason or "")


@pytest.mark.unit
async def test_operator_hint_terminal_directive_grant_moved_head_parks_not_reblock(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal directive+grant resume whose CLI MOVED HEAD off the preserved commit
    (e.g. it reset back to the remote PR head, dropping the preserved protected commit,
    then reported needs_human) must NOT re-block. Re-blocking would re-record the moved
    HEAD as the preserved marker — overwriting the original SHA — so a later grant-only
    resume would see the replacement already on the remote, no-op, and CONSUME the grant
    while the approved protected commit never lands. Park the terminal hint instead, so
    the operator is surfaced (PRRT_kwDOSJAM6s6KVCYD). Because the preserved commit was
    dropped, the stale marker is cleared AND the single-use grant is consumed so a later
    base-sync edit cannot push to the granted protected path under the stale approval
    (PRRT_kwDOSJAM6s6KVt_Q)."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_terminal_directive_grant_moved_head",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _needs_human(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="needs_human", reason="operator must decide")

    async def _preserved_gone(**_kwargs: object) -> bool:
        # The directive CLI reset HEAD back to the remote PR head, so the preserved
        # commit is no longer reachable from the current worktree HEAD.
        return False

    async def _pause_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a moved-HEAD terminal resume must park the hint, not re-block")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _needs_human)
    monkeypatch.setattr(runner, "_preserved_commit_reachable_from_head", _preserved_gone)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause_must_not_run)

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

    # Parked (NON-failed, non-paused) so the loop keeps the workspace at
    # ``monitoring_pr`` and surfaces the terminal hint to the operator.
    assert result.failed is False
    assert result.pushed is False
    assert result.paused_into_blocked is not True
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    # The DROPPED preserved commit leaves nothing to protect, so the stale marker is
    # cleared even though a grant is still active. Otherwise a later FRESH directive —
    # which revokes that grant in ``guide_workspace`` — would leave no active grants
    # plus a stale marker and a local HEAD already on the remote, letting the
    # directive-drop restart shortcut no-op the operator's follow-up
    # (PRRT_kwDOSJAM6s6KVgwV).
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        # Clearing the marker drops ``has_preserved_protected_block`` to False, so
        # ``decide()`` no longer runs this protected resume ahead of ``SyncBase``. The
        # single-use grant approved ONLY the now-dropped preserved commit, so it is
        # consumed alongside the marker — otherwise a base advance would let SyncBase's
        # conflict-resolution edit push to the granted protected path under a stale
        # approval (a grant leak, PRRT_kwDOSJAM6s6KVt_Q). A re-introduced protected
        # change re-blocks and must be granted again.
        assert grant.consumed_at is not None


@pytest.mark.unit
async def test_terminal_directive_grant_drop_clears_stale_preserved_marker_durably(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal directive+grant DROP branch must clear the preserved marker
    DURABLY on the workspace row, not only in the in-memory ``state`` the loop flushes
    later (PRRT_kwDOSJAM6s6KWAIx, the grant-bearing sibling of
    ``test_terminal_directive_drop_clears_stale_preserved_marker_durably``).

    When a combined ``--directive ... --grant ...`` resume drops the preserved commit
    and returns a TERMINAL verdict, ``_terminal_directive_grant_reblock`` consumes the
    grant and pops the marker in memory; the follow-up
    ``_clear_dropped_preserved_marker_after_terminal_directive`` early-returns (its
    ``active_grant_specs`` arg is still truthy), so without an explicit durable clear
    nothing persists the removal. After the grant is consumed a crash before the loop's
    later ``_persist_state`` would reload the pending directive with NO active grants
    plus the STALE marker — and the directive-drop restart shortcut would finalize the
    next guide as a no-op, SKIP the CLI, and swallow the terminal verdict. Assert the
    marker is gone from the persisted ``monitor_threads_addressed`` row WITHOUT any
    ``_persist_state`` flush."""
    from awf.db.models import OperatorGrantAuditRecord

    workspace_id = await seed_monitoring_workspace(factory)
    grant_id = await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    # The marker is DURABLE on the row (recorded at block time), not just in memory.
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get_for_update(workspace_id)
        assert workspace is not None
        workspace.monitor_threads_addressed = {
            _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY: "preserved-granted-sha"
        }
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_terminal_directive_grant_drop_durable",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _needs_human(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="needs_human", reason="operator must decide")

    async def _preserved_gone(**_kwargs: object) -> bool:
        return False

    async def _fail_persist_state(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("durable clear must not rely on the outer _persist_state flush")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _needs_human)
    monkeypatch.setattr(runner, "_preserved_commit_reachable_from_head", _preserved_gone)
    monkeypatch.setattr(runner, "_persist_state", _fail_persist_state)

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

    assert result.failed is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    # The marker is gone from the persisted row WITHOUT any later ``_persist_state``
    # flush, and the single-use grant is consumed.
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in (
            workspace.monitor_threads_addressed or {}
        )
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is not None
