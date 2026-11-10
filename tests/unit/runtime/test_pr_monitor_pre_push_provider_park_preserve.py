"""A managed-key park in the pre-push commit sink keeps the repair's edits.

``ProviderManagedKeyParkedError`` subclasses ``ProviderRecoveryAuthError``, so a
park raised by the protected-scope repair inside ``_commit_dirty_worktree`` used
to enter the pre-push fix-pass and dirty-finalize auth handlers, which roll the
uncommitted repair output back before re-raising. Raising the budget then
resumed from a tree without that work (PRRT_kwDOSJAM6s618qEs). The park now
skips the rollback and marks the monitor state, so the resumed hint cycle
commits the stranded edits before its pre-existing-dirty guard runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner import pre_push_validation_dirty_finalize as finalize_module
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass as fix_pass_module
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.types import ProviderManagedKeyParkedError
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    ValidationWorktreeCheck,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _mark_git_worktree,
    _name_status_z,
    _validation_result,
)

_START_HEAD = "6" * 40
_IN_SINK_HEAD = "a" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_fix_pass_sink_park_keeps_edits_and_marks_the_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix pass's edits survive a park raised by the in-sink protected-scope repair."""
    workspace_id = await seed_monitoring_workspace(factory)
    _mark_git_worktree(tmp_path / "worktrees" / workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    head = {"sha": _START_HEAD}

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return head["sha"]

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        # The protected-scope repair self-commits, then its next run hits the budget.
        head["sha"] = _IN_SINK_HEAD
        raise ProviderManagedKeyParkedError("PROVIDER_BUDGET_EXHAUSTED")

    rollback = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(fix_pass_module, "verify_head_object_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(
        pre_push_validation_module, "_rollback_failed_pre_push_validation_fix_pass", rollback
    )
    state = MonitorState()

    with pytest.raises(ProviderManagedKeyParkedError):
        await pre_push_validation_module._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=state,
            validation_result=pre_push_validation_module._PrePushValidationResult(
                passed=False,
                validation_run_id="vr_failed",
                workspace_head_sha=_START_HEAD,
                reason_code="PRE_PUSH_VALIDATION_FAILED",
                message="PR monitor pre-push validation failed: COMMAND_FAILED",
                validation_reason_code="COMMAND_FAILED",
                result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
            ),
            pass_number=1,
            total_passes=3,
            validation_commands=("pytest -q",),
        )

    rollback.assert_not_awaited()
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _IN_SINK_HEAD


@pytest.mark.unit
async def test_dirty_finalize_park_keeps_residue_and_marks_the_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair output being finalized survives a park raised inside the commit sink."""
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    # Operation-owned committed delta covers the dirty path, so the finalize commits.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rollback = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProviderManagedKeyParkedError("PROVIDER_MANAGED_KEY_UNAVAILABLE")),
    )
    monkeypatch.setattr(runner, "_rev_parse_head", AsyncMock(return_value=_IN_SINK_HEAD))
    monkeypatch.setattr(
        finalize_module, "_rollback_finalize_dirty_residue_before_provider_recovery", rollback
    )
    state = MonitorState()

    with pytest.raises(ProviderManagedKeyParkedError):
        await finalize_module._try_finalize_pre_push_dirty_repair_state(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=state,
            check=ValidationWorktreeCheck(
                clean=False,
                paths=("src/fix.py",),
                reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            ),
            operation_start_head=_START_HEAD,
            finalize_start_head=_START_HEAD,
        )

    rollback.assert_not_awaited()
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _IN_SINK_HEAD
