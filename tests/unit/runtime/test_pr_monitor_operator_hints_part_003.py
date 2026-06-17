"""Regression tests for operator remonitor hints — directive leak slice (part 3).

Split out of ``test_pr_monitor_operator_hints_part_002`` to keep that module under
the first-party line limit. These cases cover the directive-resume guard that
refuses to publish the preserved ungranted protected commit when a directive
resolves the block by reverting ON TOP of it rather than dropping it
(PRRT_kwDOSJAM6s6KFytV / comment 4512006075).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
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
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
from awf.service.controls import WorkspaceControlService
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
        session.add(
            OperatorGrantAuditRecord(
                id=grant_id,
                workspace_id=workspace_id,
                operator="op@example.com",
                reason="approved keeping the protected change",
                normalized_path=grant_path,
                block_epoch=0,
                approve_policy_downgrade=True,
            )
        )
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_violations = [{"path": path} for path in violation_paths]
        await session.commit()
    return grant_id


@pytest.mark.unit
async def test_operator_hint_directive_with_covering_grant_keeps_preserved_commit_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A combined ``--directive ... --grant ...`` resume that intentionally KEEPS the
    preserved protected edit (the grant covers every block-violation path) while the
    directive fixes other files must NOT be wedged at needs_human. The same-request
    grant authorizes publishing the preserved commit, so the leak guard short-circuits
    BEFORE the range check and the validated push proceeds (PRRT_kwDOSJAM6s6KG1hs)."""
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
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_directive_plus_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _range_must_not_run(**_kwargs: object) -> bool:
        pytest.fail("the leak guard must short-circuit on a covering grant")

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "pushed-granted-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _range_must_not_run)
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
    assert result.failed is False
    assert push_calls, "the granted keep-the-preserved-edit directive push must run"
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_directive_with_uncovering_grant_reblocks_for_grant(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directive resume whose active grant covers a DIFFERENT path than the block's
    protected violation still leaves the preserved protected change ungranted. The
    leak guard must keep firing — a partial/mismatched grant cannot authorize
    publishing the ungranted preserved commit (PRRT_kwDOSJAM6s6KG1hs guards against
    over-honoring grants) — and now RE-BLOCKS the workspace so the operator can issue
    the approve-and-keep grant the leak message advertises (which ``guide_workspace``
    only accepts for a ``blocked`` workspace). The push must NOT run, and the block is
    rebuilt from the recorded ``block_violations`` (PR #609 codex P2 thread)."""
    workspace_id = await seed_monitoring_workspace(factory)
    # The empty-path entry exercises the helper's defensive skip; only the real
    # protected path is rebuilt into the re-block.
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="setup.cfg",
        violation_paths=("", "pyproject.toml"),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert pyproject.toml",
        directive="revert it",
        operation_id="op_directive_uncovering_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-ungranted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-ungranted-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_in_range(**_kwargs: object) -> bool:
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

    async def _validated_push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a partial-grant leak must be refused before push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push_must_not_run)

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
    # The block is rebuilt from the recorded ``block_violations`` (the preserved
    # commit's protected path), NOT the partial/mismatched grant path.
    leak_block = captured["protected_scope_block"]
    assert isinstance(leak_block, _ProtectedScopePushBlock)
    assert [v.path for v in leak_block.violations] == ["pyproject.toml"]
    assert "preserved-ungranted-sha" in leak_block.message
    # No sync-base origin was recorded, so the re-block records the generic phase.
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    # A landed re-block clears the in-memory monitor hint (the bumped block epoch
    # supersedes it; the resume re-arms a fresh one).
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_directive_revert_on_top_leaks_preserved_commit_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIRECTIVE that resolves a protected block by adding a revert commit ON TOP
    of the preserved offending commit leaves the net tree matching the remote PR
    branch, so ``_protected_scope_push_block`` is None. Pushing HEAD would still
    fast-forward the remote branch over the ungranted protected-file commit plus its
    revert. The directive guard detects the preserved commit is still in the pushed
    range and surfaces needs_human WITHOUT pushing (comment 4512006075)."""
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
        operation_id="op_directive_revert_on_top",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-offending-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-offending-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_in_range(**_kwargs: object) -> bool:
        return True

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("leaking directive push must be refused before validation/push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated_push_must_not_run)

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
    # Refuse the leak WITHOUT terminally failing: the guard surfaces needs_human
    # and the loop must park the workspace at ``monitoring_pr`` awaiting the
    # operator, not ``_terminate_failed`` it (PRRT_kwDOSJAM6s6KHEEU).
    assert result.failed is False
    assert result.terminal_monitor_failure is False
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert "preserved-offending-sha" in (state.pending_operator_hint.status_reason or "")
    # The preserved marker is retained so a corrected resume keeps the divergence
    # context; it is only dropped once the resume is finalized.
    assert (
        state.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY)
        == "preserved-offending-sha"
    )


@pytest.mark.unit
async def test_operator_hint_directive_revert_dropped_preserved_commit_pushes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the directive dropped the preserved commit (reset away), the guard does
    not fire and the validated push proceeds: the legitimate revert path is not
    wedged by the leak guard."""
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
        operation_id="op_directive_revert_dropped",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "dropped-preserved-sha")
    push_calls: list[dict[str, object]] = []

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("redone-head-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_not_in_range(**_kwargs: object) -> bool:
        return False

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _validated_push(**kwargs: object) -> _GitPushResult:
        push_calls.append(kwargs)
        return _GitPushResult(pushed=True, failed=False, returncode=0)

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "redone-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_not_in_range)
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
    assert result.failed is False
    assert push_calls, "the legitimate dropped-commit directive push must run"
    assert state.pending_operator_hint is None


def _guide_service(session: AsyncSession) -> WorkspaceControlService:
    """Build a control service for the guide-resolves regression (no stack ops)."""

    async def _unused_stopper(_compose_project_name: str | None) -> None:  # pragma: no cover
        raise AssertionError("guide must not stop the stack")

    def _unused_cleaner_factory() -> object:  # pragma: no cover
        raise AssertionError("guide must not clean up the workspace")

    return WorkspaceControlService(
        session,
        project_stopper=_unused_stopper,
        cleaner_factory=_unused_cleaner_factory,
    )


@pytest.mark.unit
async def test_directive_leak_reblock_lets_operator_grant_resolve_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: after the directive-leak guard fires it RE-BLOCKS the workspace,
    and an operator approve-and-keep ``guide --grant`` is then accepted and resolves
    it. Before this fix the guard parked a needs_human hint at ``monitoring_pr``,
    where ``guide_workspace`` rejects grant inputs (``WorkspaceGuideGrantNotAllowedError``)
    — so the advertised approve-and-keep was unreachable (PR #609 codex P2 thread)."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        # A fully-detailed recorded violation exercises the helper's section/line/
        # protected_pattern/reason reconstruction.
        workspace.block_violations = [
            {
                "path": "pyproject.toml",
                "protected_pattern": "pyproject.toml",
                "section": "tool.coverage",
                "line": 5,
                "reason": "coverage threshold weakened",
            }
        ]
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason="revert pyproject.toml",
        directive="revert it",
        operation_id="op_directive_leak_then_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-offending-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-offending-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _preserved_in_range(**_kwargs: object) -> bool:
        return True

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "preserved-offending-sha"

    async def _no_notification(**_kwargs: object) -> None:
        return None

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("the leak re-block must NOT push")

    # Run the REAL ``_pause_monitor_for_protected_scope_block`` so the workspace
    # actually transitions ``monitoring_pr -> blocked`` in the DB; only the git HEAD
    # read and the best-effort PR notification are stubbed.
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_in_unpushed_range", _preserved_in_range)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)
    monkeypatch.setattr(runner, "_post_protected_block_notification", _no_notification)

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
    assert state.pending_operator_hint is None

    # The guard re-blocked the workspace: status is now ``blocked`` (the precondition
    # that makes ``guide --grant`` acceptable) with a monitor-origin resume phase.
    async with factory() as session:
        blocked = await WorkspaceRepository(session).get(workspace_id)
        assert blocked is not None
        assert blocked.status == WorkspaceStatus.blocked.value
        assert blocked.block_resume_phase == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
        blocked_version = blocked.version

    # The operator approve-and-keep grant is now accepted (it raised
    # ``WorkspaceGuideGrantNotAllowedError`` while parked at ``monitoring_pr``) and
    # resolves the workspace back into the monitor for a grant-aware push.
    async with factory() as session:
        service = _guide_service(session)
        response = await service.guide_workspace(
            workspace_id,
            directive="",
            reason="approve keeping the protected pyproject.toml change",
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            operator="op@example.com",
            idempotency_key="guide-after-leak-reblock",
            expected_version=blocked_version,
        )
        await session.commit()
        assert response.status == WorkspaceStatus.monitoring_pr

    async with factory() as session:
        resolved = await WorkspaceRepository(session).get(workspace_id)
        assert resolved is not None
        assert resolved.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
@pytest.mark.parametrize("terminal_verdict", ["agent_failed", "needs_human"])
async def test_operator_hint_terminal_directive_with_grant_reblocks(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_verdict: str,
) -> None:
    """A combined ``--directive ... --grant ...`` resume of a monitor-origin protected
    block whose directive CLI returns a TERMINAL verdict BEFORE any push must NOT
    merely park the hint at ``monitoring_pr``: the preserved commit and the
    approve-and-keep grant stay unfinalized, ``decide()`` only surfaces NotifyHuman,
    and ``guide_workspace`` rejects new grants unless the row is ``blocked`` while a
    new directive revokes the existing grant — stranding the operator. Re-block so
    ``guide --grant`` / a new directive can resolve it, rebuilding the block from the
    recorded ``block_violations`` and clearing the in-memory hint
    (PRRT_kwDOSJAM6s6KT0rd)."""
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
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_terminal_directive_grant",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _terminal(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict=terminal_verdict, reason="agent could not finish")

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
        pytest.fail("a terminal directive outcome must re-block, not push")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _terminal)
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
    # The block is rebuilt from the recorded ``block_violations``, and the message
    # carries the terminal verdict and preserved SHA for operator context.
    leak_block = captured["protected_scope_block"]
    assert isinstance(leak_block, _ProtectedScopePushBlock)
    assert [v.path for v in leak_block.violations] == ["pyproject.toml"]
    assert "preserved-granted-sha" in leak_block.message
    assert terminal_verdict in leak_block.message
    # No sync-base origin was recorded, so the re-block records the generic phase.
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    # A landed re-block clears the in-memory monitor hint (the bumped block epoch
    # supersedes it; the resume re-arms a fresh one).
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_terminal_directive_grant_reblock_lets_operator_resolve(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: after a terminal directive+grant resume re-blocks the workspace, an
    operator ``guide --grant`` is accepted and resolves it. Before this fix the
    terminal branch parked a needs_human/agent_failed hint at ``monitoring_pr`` where
    ``guide_workspace`` rejects grant inputs — so the operator could never land the
    preserved commit (PRRT_kwDOSJAM6s6KT0rd)."""
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
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_terminal_directive_grant_e2e",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _agent_failed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="agent_failed", reason="adapter crashed")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "preserved-granted-sha"

    async def _no_notification(**_kwargs: object) -> None:
        return None

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("a terminal directive outcome must re-block, not push")

    # Run the REAL ``_pause_monitor_for_protected_scope_block`` so the workspace
    # actually transitions ``monitoring_pr -> blocked`` in the DB.
    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _agent_failed)
    monkeypatch.setattr(runner, "_validated_git_push_result", _push_must_not_run)
    monkeypatch.setattr(runner, "_rev_parse_head", _head)
    monkeypatch.setattr(runner, "_post_protected_block_notification", _no_notification)

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
    assert state.pending_operator_hint is None

    async with factory() as session:
        blocked = await WorkspaceRepository(session).get(workspace_id)
        assert blocked is not None
        assert blocked.status == WorkspaceStatus.blocked.value
        assert blocked.block_resume_phase == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
        blocked_version = blocked.version

    # The operator approve-and-keep grant is now accepted (it raised
    # ``WorkspaceGuideGrantNotAllowedError`` while parked at ``monitoring_pr``) and
    # resolves the workspace back into the monitor for a grant-aware push.
    async with factory() as session:
        service = _guide_service(session)
        response = await service.guide_workspace(
            workspace_id,
            directive="",
            reason="approve keeping the protected pyproject.toml change",
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            operator="op@example.com",
            idempotency_key="guide-after-terminal-reblock",
            expected_version=blocked_version,
        )
        await session.commit()
        assert response.status == WorkspaceStatus.monitoring_pr


@pytest.mark.unit
async def test_operator_hint_terminal_directive_grant_reblock_preserves_sync_base_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal directive+grant resume of a SYNC-BASE-originated block must re-block
    with the ``monitor_protected_scope_sync_base`` phase again — preserving the resume
    ORIGIN exactly as the net-diff / leak re-block branches do — so the next resume
    keeps selecting the sync-base-aware validator (PRRT_kwDOSJAM6s6KT0rd)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=("pyproject.toml",),
    )
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.block_resume_phase = MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
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
        operation_id="op_terminal_directive_grant_sync_base",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _agent_failed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="agent_failed", reason="adapter crashed")

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

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _agent_failed)
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

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_terminal_directive_grant_stale_cas_keeps_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the re-block pause loses the CAS (the row already left ``monitoring_pr``),
    ``_pause_monitor_for_protected_scope_block`` returns a NON-paused failed result.
    The terminal-reblock helper must surface that result WITHOUT clearing the
    in-memory hint, so the caller's normal failed handling runs against whatever
    terminal state already won (PRRT_kwDOSJAM6s6KT0rd)."""
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
        reason="keep pyproject.toml and fix the rest",
        directive="redo the unrelated files but keep the protected edit",
        operation_id="op_terminal_directive_grant_stale",
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
        return VerdictResult(verdict="needs_human", reason="agent asked for help")

    async def _pause_stale_cas(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            paused_into_blocked=False,
        )

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _needs_human)
    monkeypatch.setattr(runner, "_pause_monitor_for_protected_scope_block", _pause_stale_cas)

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

    assert result.paused_into_blocked is False
    assert result.failed is True
    # A lost CAS must NOT clear the hint — the row already transitioned elsewhere.
    assert state.pending_operator_hint is hint


@pytest.mark.unit
async def test_operator_hint_terminal_directive_grant_no_violation_parks_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive fallback: when no protected violation can be reconstructed (no
    recorded ``block_violations``), the terminal-reblock helper returns ``None`` and
    the caller falls back to the prior behavior — mark the terminal hint and return a
    NON-failed result so the loop parks the recoverable workspace at ``monitoring_pr``
    (PRRT_kwDOSJAM6s6KT0rd)."""
    workspace_id = await seed_monitoring_workspace(factory)
    # Grant present but NO recorded block_violations to rebuild from.
    await _seed_grant_and_block_violations(
        factory,
        workspace_id,
        grant_path="pyproject.toml",
        violation_paths=(),
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
        operation_id="op_terminal_directive_grant_no_violation",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-granted-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-granted-sha", None)

    async def _agent_failed(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="agent_failed", reason="adapter crashed")

    async def _pause_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("no reconstructable violation must NOT re-block")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _agent_failed)
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

    assert result == _GitPushResult(pushed=False, failed=False, returncode=0)
    # Falls back to parking the terminal hint at monitoring_pr.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_missing_inputs_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """No preserved SHA, or a missing worktree, cannot leak: return False without
    touching git."""
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_present"
    worktree.mkdir(parents=True)

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_present",
            worktree_path=worktree,
            remote_branch="awf/ws_present",
            preserved_head_sha=None,
        )
        is False
    )
    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_missing",
            worktree_path=tmp_path / "worktrees" / "ws_missing",
            remote_branch="awf/ws_missing",
            preserved_head_sha="preserved-sha",
        )
        is False
    )


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_reset_away_returns_false(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A preserved commit no longer reachable from HEAD (the directive reset it
    away) is not in the pushed range — no fetch is needed."""
    worktree = tmp_path / "worktrees" / "ws_reset"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    # merge-base --is-ancestor <preserved> HEAD exits non-zero: not an ancestor.
    cmd.queue_result(returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_reset",
            worktree_path=worktree,
            remote_branch="awf/ws_reset",
            preserved_head_sha="preserved-sha",
        )
        is False
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 1
    assert "preserved-sha" in ancestry_calls[0].args
    assert "HEAD" in ancestry_calls[0].args


@pytest.mark.unit
async def test_preserved_commit_in_unpushed_range_diff_error_fails_closed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the preserved commit is confirmed an ancestor of HEAD, a containment
    fetch failure cannot prove it is already published, so fail closed (True) to
    surface needs_human rather than leak the ungranted protected commit."""
    worktree = tmp_path / "worktrees" / "ws_err"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # ancestor of HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _diff(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        raise ProtectedScopeDiffError("fetch failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _diff)

    assert (
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_err",
            worktree_path=worktree,
            remote_branch="awf/ws_err",
            preserved_head_sha="preserved-sha",
        )
        is True
    )
    # Failing the fetch must not reach the FETCH_HEAD containment merge-base: only
    # the ancestry check ran before fail-closed.
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 1
    assert "HEAD" in ancestry_calls[0].args


@pytest.mark.unit
@pytest.mark.parametrize(
    ("on_remote_returncode", "expected"),
    [
        (1, True),  # preserved NOT on remote: still in the unpushed range -> leak
        (0, False),  # preserved already on remote: cannot be un-leaked
    ],
)
async def test_preserved_commit_in_unpushed_range_on_remote_decides_outcome(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_remote_returncode: int,
    expected: bool,
) -> None:
    """An ancestor-of-HEAD preserved commit leaks only when it is not already
    contained in the freshly-fetched remote PR head."""
    worktree = tmp_path / "worktrees" / "ws_range"
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # merge-base --is-ancestor <preserved> HEAD: ancestor
    cmd.queue_result(returncode=on_remote_returncode)  # ... <preserved> FETCH_HEAD
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
        await runner._preserved_commit_in_unpushed_range(
            workspace_id="ws_range",
            worktree_path=worktree,
            remote_branch="awf/ws_range",
            preserved_head_sha="preserved-sha",
        )
        is expected
    )
    ancestry_calls = [c for c in cmd.calls if "--is-ancestor" in c.args]
    assert len(ancestry_calls) == 2
    assert "HEAD" in ancestry_calls[0].args
    assert "FETCH_HEAD" in ancestry_calls[1].args


@pytest.mark.unit
async def test_preserved_protected_change_fully_granted_edge_cases(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The grant-coverage helper fails closed: no grants, a vanished workspace, or a
    block with no recorded violation paths all return False so the leak guard keeps
    firing rather than over-honoring a grant (PRRT_kwDOSJAM6s6KG1hs)."""
    from awf.control.quality_gates import GrantSpec

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    grant = GrantSpec(path="pyproject.toml", approve_policy_downgrade=True)

    # No grants at all -> ungranted.
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id="ws_missing", grant_specs=[]
        )
        is False
    )
    # Grant present but the workspace row is gone -> cannot confirm coverage.
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id="ws_missing", grant_specs=[grant]
        )
        is False
    )
    # Grant present but the block recorded no violation paths -> nothing to confirm
    # the grant authorizes keeping, so fail closed.
    workspace_id = await seed_monitoring_workspace(factory)
    assert (
        await runner._preserved_protected_change_fully_granted(
            workspace_id=workspace_id, grant_specs=[grant]
        )
        is False
    )


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
