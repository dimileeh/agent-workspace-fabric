"""Regression tests for operator remonitor hints — grant/resume slice (part 2)."""

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
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.operator_hints import (
    _mark_referenced_needs_human_feedback_answered,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"
REVIEW_COMMENT_ID = "issue:3476977020"
REVIEW_COMMENT_BODY_HASH_KEY = f"__review_comment_body_hash__:{REVIEW_COMMENT_ID}"
REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY = f"__needs_human_reason__:{REVIEW_COMMENT_ID}"


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
def test_directiveless_operator_hint_reason_does_not_retire_referenced_needs_human() -> None:
    """Grant-only audit reasons are not acted-on review-feedback text.

    Approve-and-keep resumes skip the CLI, so mentioning a review id in the audit
    reason must not convert a stored ``needs_human`` review verdict to
    ``false_positive``.
    """
    state = MonitorState(
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_BODY_HASH_KEY: "body-hash",
            REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY: "review still needs a human",
        }
    )
    hint = OperatorHint(
        reason=f"approved protected path; related review feedback {REVIEW_COMMENT_ID}",
        operation_id="op_grant_only_mentions_review",
        requested_at="2026-06-25T19:30:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )

    _mark_referenced_needs_human_feedback_answered(state, hint=hint)

    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "needs_human"
    assert state.threads_addressed_ids[REVIEW_COMMENT_BODY_HASH_KEY] == "body-hash"
    assert (
        state.threads_addressed_ids[REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY]
        == "review still needs a human"
    )


@pytest.mark.unit
def test_operator_hint_directive_retires_referenced_needs_human() -> None:
    """A directive is acted-on text and may retire the referenced review verdict."""
    state = MonitorState(
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_BODY_HASH_KEY: "body-hash",
            REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY: "review still needs a human",
        }
    )
    hint = OperatorHint(
        reason="operator guide audit note",
        directive=f"Resolve {REVIEW_COMMENT_ID} as false positive after checking the code.",
        operation_id="op_directive_mentions_review",
        requested_at="2026-06-25T19:35:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )

    _mark_referenced_needs_human_feedback_answered(state, hint=hint)

    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "false_positive"
    assert state.threads_addressed_ids[REVIEW_COMMENT_BODY_HASH_KEY] == "body-hash"
    assert REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_operator_hint_directive_retires_issue_feedback_from_bare_id() -> None:
    """Contextual bare feedback ids also match GitHub ``issue:<databaseId>`` keys."""
    bare_comment_id = REVIEW_COMMENT_ID.removeprefix("issue:")
    state = MonitorState(
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_BODY_HASH_KEY: "body-hash",
            REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY: "review still needs a human",
        }
    )
    hint = OperatorHint(
        reason="operator guide audit note",
        directive=f"Resolve feedback id {bare_comment_id} as false positive.",
        operation_id="op_directive_mentions_bare_review_id",
        requested_at="2026-06-25T20:25:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )

    _mark_referenced_needs_human_feedback_answered(state, hint=hint)

    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "false_positive"
    assert state.threads_addressed_ids[REVIEW_COMMENT_BODY_HASH_KEY] == "body-hash"
    assert REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_operator_hint_directive_ignores_uncontextualized_long_number() -> None:
    """Bare long numbers need feedback/comment-id context before matching."""
    bare_comment_id = REVIEW_COMMENT_ID.removeprefix("issue:")
    state = MonitorState(
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_BODY_HASH_KEY: "body-hash",
            REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY: "review still needs a human",
        }
    )
    hint = OperatorHint(
        reason="operator guide audit note",
        directive=f"Investigated build {bare_comment_id} and found no code issue.",
        operation_id="op_directive_mentions_uncontextualized_number",
        requested_at="2026-06-25T20:30:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )

    _mark_referenced_needs_human_feedback_answered(state, hint=hint)

    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "needs_human"
    assert state.threads_addressed_ids[REVIEW_COMMENT_BODY_HASH_KEY] == "body-hash"
    assert (
        state.threads_addressed_ids[REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY]
        == "review still needs a human"
    )


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
async def test_operator_hint_resume_clears_block_resume_phase_after_finalize(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a sync-base-originated protected block resumes and its repair pushes,
    the workspace's ``block_resume_phase`` must be cleared. Otherwise a later
    operator-hint / remonitor cycle on ``monitoring_pr`` — which arms a fresh hint
    WITHOUT re-blocking, so it never re-records the phase — would still read the
    stale ``monitor_protected_scope_sync_base`` value and select the sync-base-aware
    validator. That validator drops paths whose final tree matches the merged base,
    so a repair that reverts an unowned protected file back to base contents would
    push without a grant or a re-block (PRRT_kwDOSJAM6s6KFqEg)."""
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
        operation_id="op_clear_phase",
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
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pushed is True
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    assert workspace.block_resume_phase is None


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
    # No sync-base origin was recorded, so the re-block records the generic
    # ``monitor_protected_scope_push`` phase (default).
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    # A landed re-block must clear the in-memory monitor hint so the state the
    # loop persists afterward does not show a pending resume while the workspace
    # is already ``blocked`` (the bumped block epoch supersedes this hint).
    assert state.pending_operator_hint is None


@pytest.mark.unit
async def test_operator_hint_reblock_preserves_sync_base_resume_phase(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-blocking a sync-base-originated resume must KEEP the sync-base phase.

    When a ``monitor_protected_scope_sync_base`` block is resumed and the directive
    still leaves a violation, the re-block must record the sync-base phase again —
    NOT fall back to the default ``monitor_protected_scope_push``. Otherwise the
    next operator resume would select the generic unpushed-commit validator and
    re-block on a target-branch-owned protected change the base merge pulled in,
    even after the agent-authored protected edit was correctly reverted
    (PRRT_kwDOSJAM6s6KGCXl)."""
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
        operation_id="op_reblock_sync_base",
        requested_at="2026-06-17T00:00:00+00:00",
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
        base_branch="main",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.paused_into_blocked is True
    assert captured["resume_phase"] == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE


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
    the remote treats the push as a no-op (no duplicate push). The preserved-head
    marker is present (recorded durably at block time, cleared only by a finalize that
    also consumes the grant), so the SHA-containment proof is available and the no-op
    safely consumes the grant — the no-marker variant instead surfaces needs_human."""
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
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-sha")

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
async def test_operator_hint_directive_revert_no_op_push_finishes_without_preserved_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIRECTIVE revert can resolve a protected block by resetting the worktree
    back to the remote PR head, removing the preserved local commit. The CLI ran,
    the violation is cleared, and an up-to-date no-op push is a valid successful
    revert. The missing-preserved-commit guard is scoped to grant-only
    approve-and-keep resumes, so a directive revert must finish the bookkeeping
    instead of being wedged at needs_human (PRRT_kwDOSJAM6s6KFytV)."""
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
        operation_id="op_directive_revert_noop",
        requested_at="2026-06-17T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    # The block recorded the preserved HEAD; the revert removed that commit.
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "reverted-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

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
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
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
    # The applied directive is finished, not wedged at needs_human.
    assert state.pending_operator_hint is None


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
async def test_operator_hint_grant_only_no_op_without_marker_surfaces_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idempotent no-op shortcut must NOT consume an active approve-and-keep grant
    when no preserved-head marker is recorded. ``_preserved_commit_already_on_remote``
    only runs its positive SHA-containment proof when ``preserved_head_sha`` is set;
    with the marker missing it returns True on an empty worktree↔remote diff ALONE, so
    a worktree reset to the remote head would let the early shortcut consume the
    single-use grant while the approved protected commit never landed. Keep the grant
    active and surface needs_human instead, mirroring the post-push EtU2 fall-through
    guard (PR #609 comment 4521107313)."""
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
        operation_id="op_no_marker_no_op",
        requested_at="2026-06-18T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    # Deliberately NO preserved-head marker recorded on the state.
    state = MonitorState(pending_operator_hint=hint)

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("abc1234567890def", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("a grant-only resume must NOT invoke the CLI")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _already_on_remote(**_kwargs: object) -> bool:
        # Empty worktree↔remote diff with NO marker: True on the diff alone.
        return True

    async def _push_must_not_run(**_kwargs: object) -> _GitPushResult:
        pytest.fail("an unproven no-op must NOT consume the grant via a push/finalize")

    async def _head(*_args: object, **_kwargs: object) -> str:
        return "reset-to-remote-head-sha"

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _cli_must_not_run)
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
    # The hint is NOT processed — it is surfaced for human attention instead.
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    # The grant is NOT consumed: the approved protected change was never proven landed.
    async with factory() as session:
        grant = await session.get(OperatorGrantAuditRecord, grant_id)
        assert grant is not None
        assert grant.consumed_at is None


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
async def test_operator_hint_remonitor_preserved_head_shortcut_keeps_review_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale preserved-head shortcut must not retire review feedback.

    A directiveless remonitor whose reason names a review id has not acted on that
    feedback when the restart shortcut skips the CLI, so the review-level
    ``needs_human`` verdict must remain blocking.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    hint = OperatorHint(
        reason=f"please re-check {REVIEW_COMMENT_ID}",
        operation_id="op_stale_marker_remonitor_mentions_review",
        requested_at="2026-06-25T20:30:00+00:00",
        reason_code="OPERATOR_REMONITOR",
    )
    state = MonitorState(
        pending_operator_hint=hint,
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_BODY_HASH_KEY: "body-hash",
            REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY: "review still needs a human",
        },
    )
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "recorded-preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("recorded-preserved-sha", None)

    async def _cli_must_not_run(**_kwargs: object) -> object:
        pytest.fail("the stale preserved-head shortcut should skip the CLI")

    async def _already_on_remote(**_kwargs: object) -> bool:
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
    assert state.pending_operator_hint is None
    assert _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "needs_human"
    assert state.threads_addressed_ids[REVIEW_COMMENT_BODY_HASH_KEY] == "body-hash"
    assert (
        state.threads_addressed_ids[REVIEW_COMMENT_NEEDS_HUMAN_REASON_KEY]
        == "review still needs a human"
    )


@pytest.mark.unit
async def test_operator_hint_directive_grant_consumed_restart_skips_cli_when_commit_on_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart-after-consume recovery for a combined ``--directive ... --grant ...``
    resume (PRRT_kwDOSJAM6s6KUNoL): the resume runs the directive CLI, pushes the
    preserved commit, consumes the single-use grant (committed immediately), then
    crashes BEFORE the processed marker persists. On restart the grant is gone but
    the durable preserved-head marker remains and the approved commit is already on
    the remote. A directive hint must take the SAME short-circuit as a grant-only
    resume — finalize the bookkeeping WITHOUT re-invoking the directive CLI, which
    would otherwise run without the consumed grant and create extra unapproved work
    or re-block an already resolved pause."""
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
        reason="approved the protected change and asked to fix the rest",
        directive="keep the protected edit, fix the remaining files",
        operation_id="op_directive_grant_consumed_restart",
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
        pytest.fail("a consumed-grant directive restart must NOT re-invoke the CLI")

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
    assert captured.get("preserved_head_sha") == "recorded-preserved-sha"
    assert state.last_push_sha == "recorded-preserved-sha"
    assert state.pending_operator_hint is None  # hint processed without the agent
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
