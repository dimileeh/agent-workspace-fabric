"""#910 follow-up: the terminal guard must re-arm AFTER pre-push validation.

Every monitor action seam re-reads PR state before pushing, but ``_validated_git_push_result``
then runs the profile's validation suite (plus its agent fix passes) — the longest
remaining step in the cycle. A PR that merged or closed while that ran would still be
pushed, reopening exactly the race the guard closes (PRRT_kwDOSJAM6s6fjOze).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceEventRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    OperatorHint,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult
from awf.runtime.pr_monitor_runner.constants import _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import _PostActionPrTerminalState
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _set_resolved_profile,
    _validation_result,
)

MOOT_EVENT = "workspace.monitor_action_moot"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status(*, merged: bool = False, closed: bool = False) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        base_ref="development",
        merge_state_status=MergeStateStatus.CLEAN,
        merged=merged,
        closed=closed or merged,
        merge_commit_sha="mergesha0000" if merged else None,
    )


class _StatusGh:
    """Forge double returning one fixed ``PRStatus`` and recording fetches."""

    def __init__(self, status: PRStatus) -> None:
        """Store the snapshot every ``fetch_pr_status`` call returns."""
        self._status = status
        self.fetches: list[int] = []

    async def fetch_pr_status(
        self,
        *,
        repo: object,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Record the round-trip and return the fixed snapshot."""
        del repo, base_behind_count, retry
        self.fetches.append(pr_number)
        return self._status

    async def aclose(self) -> None:
        """Match the single-use forge-client lifecycle the runner closes."""


async def _moot_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[object]:
    async with factory() as session:
        return list(
            await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type=MOOT_EVENT,
                limit=10,
            )
        )


async def _validated_push_against(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    status: PRStatus,
) -> tuple[str, _GitPushResult, FakeCommandRunner, _StatusGh]:
    """Run ``_validated_git_push_result`` with passing validation against ``status``."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.respond_when(lambda args: "rev-parse" in args, stdout="unpushed-repair-head\n")
    gh = _StatusGh(status)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        pr_number=42,
        pr_terminal_context="comment_repair",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        operation_id="op_fix",
        operation_type="comment_repair",
    )
    return workspace_id, result, cmd, gh


@pytest.mark.unit
async def test_validated_push_is_moot_when_pr_merges_during_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A PR that merged WHILE pre-push validation ran must not be pushed to.

    The seam's own guard already observed the PR as open before validation started,
    so without the post-validation recheck the repair is published onto a merged PR.
    """
    workspace_id, result, cmd, gh = await _validated_push_against(
        factory, tmp_path, status=_status(merged=True)
    )

    assert result.pushed is False
    assert result.failed is False
    assert result.pr_terminal is not None
    assert result.reason_code == _MONITOR_ACTION_MOOT_PR_TERMINAL_REASON
    assert not any("push" in call.args for call in cmd.calls)
    assert gh.fetches == [42]

    events = await _moot_events(factory, workspace_id)
    assert len(events) == 1
    payload = events[0].payload  # type: ignore[attr-defined]
    assert payload["context"] == "comment_repair_post_validation"
    assert payload["pr_state"] == "merged"
    assert payload["local_head_sha"] == "unpushed-repair-head"
    assert payload["pushed"] is False


@pytest.mark.unit
async def test_validated_push_proceeds_when_pr_still_open_after_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The recheck is a confirmation, not a gate: an open PR still gets its push."""
    workspace_id, result, cmd, gh = await _validated_push_against(
        factory, tmp_path, status=_status()
    )

    assert result.pr_terminal is None
    assert any("push" in call.args for call in cmd.calls)
    assert gh.fetches == [42]
    assert await _moot_events(factory, workspace_id) == []


@pytest.mark.unit
async def test_validated_push_skips_recheck_without_pr_context(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Callers with no PR context keep the pre-guard behavior (no extra round-trip)."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.respond_when(lambda args: "rev-parse" in args, stdout="unpushed-repair-head\n")
    gh = _StatusGh(_status(merged=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.pr_terminal is None
    assert gh.fetches == []
    assert any("push" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_operator_hint_returns_moot_without_finalizing_the_hint(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator-hint seam must return the moot envelope, not finalize the resume.

    A moot result is ``pushed=False, failed=False``, which otherwise falls into the
    no-op-push arm that consumes the single-use grant and marks the hint processed —
    bookkeeping that must not run against a PR that merged mid-validation.
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
        reason="fix the failing test",
        directive="rerun the repair",
        operation_id="op_hint_moot",
        requested_at="2026-09-05T00:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)
    state.mark_addressed(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, "preserved-sha")

    async def _no_preexisting_dirty(**_kwargs: object) -> None:
        return None

    async def _start_head_ok(**_kwargs: object) -> tuple[str, None]:
        return ("preserved-sha", None)

    async def _fixed_verdict(**_kwargs: object) -> VerdictResult:
        return VerdictResult(verdict="fix_committed")

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _not_on_remote(**_kwargs: object) -> bool:
        return False

    async def _moot_push(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=False,
            returncode=0,
            reason_code=_MONITOR_ACTION_MOOT_PR_TERMINAL_REASON,
            pr_terminal=_PostActionPrTerminalState(
                status=_status(merged=True),
                local_head_sha="preserved-sha",
            ),
        )

    async def _finalize_must_not_run(**_kwargs: object) -> None:
        pytest.fail("a moot resume must not finalize the hint or consume the grant")

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_preexisting_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head_ok)
    monkeypatch.setattr(runner, "_invoke_cli_for_verdict_result", _fixed_verdict)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_preserved_commit_already_on_remote", _not_on_remote)
    monkeypatch.setattr(runner, "_validated_git_push_result", _moot_push)
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.operator_hints._finalize_operator_hint_resume",
        _finalize_must_not_run,
    )

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

    assert result.pr_terminal is not None
    assert result.failed is False
    assert state.pending_operator_hint is hint
