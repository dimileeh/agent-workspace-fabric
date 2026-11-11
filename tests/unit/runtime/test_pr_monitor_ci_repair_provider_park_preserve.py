"""A managed-key park in the CI-repair commit sink keeps the repair's edits.

``ProviderManagedKeyParkedError`` subclasses ``ProviderRecoveryAuthError``, so a
park during CI repair used to take the provider-recovery salvage-and-rollback
path: the stranded repair output was saved as a patch, reset and cleaned out of
the worktree, and only then did the park propagate. Raising the budget resumed
from a tree without that work and with no stranded-edits marker, so the repair
restarted from scratch (PRRT_kwDOSJAM6s619mxi). The park now skips the salvage
and rollback and marks the monitor state, so the resumed hint cycle commits the
stranded edits before its pre-existing-dirty guard runs — whether the park came
from the CI-repair agent itself or from the protected-scope repair in the sink.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import CheckFailure, MonitorState
from awf.runtime.pr_monitor_runner import ci_ops as ci_ops_module
from awf.runtime.pr_monitor_runner.constants import _PRE_EXISTING_DIRTY_WORKTREE_REASON
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    PROVIDER_PARK_STRANDED_EDITS_STATE_KEY,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import ProviderManagedKeyParkedError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_START_HEAD = "6" * 40
_POST_SINK_HEAD = "c" * 40


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _patch_ci_fix_seams(
    runner: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_agent: AsyncMock,
    commit_dirty_worktree: AsyncMock,
    dirty_results: list[_GitPushResult | None],
) -> tuple[AsyncMock, AsyncMock]:
    """Stub the CI fix's git/agent seams; return the salvage and rollback mocks."""
    salvage = AsyncMock(return_value={})
    rollback = AsyncMock()
    monkeypatch.setattr(runner, "_run_monitor_agent_with_service_recovery", run_agent)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty_worktree)
    monkeypatch.setattr(runner, "_rev_parse_head", AsyncMock(return_value=_POST_SINK_HEAD))
    monkeypatch.setattr(
        runner,
        "_pre_existing_dirty_repair_worktree_result",
        AsyncMock(side_effect=dirty_results),
    )
    monkeypatch.setattr(
        runner,
        "_repair_operation_start_head_result",
        AsyncMock(return_value=(_START_HEAD, None)),
    )
    monkeypatch.setattr(
        ci_ops_module, "repair_agent_runtime_ownership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(ci_ops_module, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(ci_ops_module, "_salvage_ci_repair_dirty_output", salvage)
    monkeypatch.setattr(
        ci_ops_module, "_rollback_ci_fix_residue_before_provider_recovery", rollback
    )
    return salvage, rollback


async def _run_ci_fix(
    runner: Any, *, workspace_id: str, tmp_path: Path, state: MonitorState
) -> None:
    await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        state=state,
    )


@pytest.mark.unit
async def test_ci_repair_agent_park_keeps_stranded_edits_and_marks_the_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edits the sink could not commit stay in place when the CI-repair agent parks."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    run_agent = AsyncMock(
        side_effect=AgentRunError(
            agent="codex",
            result=CommandResult(returncode=78, stdout="", stderr="PROVIDER_BUDGET_EXHAUSTED"),
            reason_code="PROVIDER_BUDGET_EXHAUSTED",
            details={"retryable": False, "provider_budget": {"limit_usd": 5}},
        )
    )
    stranded = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="dirty",
        reason_code=_PRE_EXISTING_DIRTY_WORKTREE_REASON,
        details={"paths": ["src/fix.py"]},
    )
    salvage, rollback = _patch_ci_fix_seams(
        runner,
        monkeypatch,
        run_agent=run_agent,
        # The sink's own ``git commit`` fails, leaving the repair output dirty.
        commit_dirty_worktree=AsyncMock(return_value=False),
        dirty_results=[None, stranded],
    )
    state = MonitorState()

    with pytest.raises(ProviderManagedKeyParkedError) as exc_info:
        await _run_ci_fix(runner, workspace_id=workspace_id, tmp_path=tmp_path, state=state)

    salvage.assert_not_awaited()
    rollback.assert_not_awaited()
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _POST_SINK_HEAD
    details = exc_info.value.details
    assert details is not None
    assert details["provider_budget"] == {"limit_usd": 5}
    assert details["phase"] == "ci_repair_commit_sink"
    assert details["stranded_paths"] == ["src/fix.py"]
    assert details["dirty_commit_error"]["preserved_head"] == _POST_SINK_HEAD


@pytest.mark.unit
async def test_ci_repair_sink_park_keeps_edits_and_marks_the_resume(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A park raised by the in-sink protected-scope repair keeps the CI repair's edits."""
    workspace_id = await seed_monitoring_workspace(factory)
    (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    salvage, rollback = _patch_ci_fix_seams(
        runner,
        monkeypatch,
        run_agent=AsyncMock(return_value=None),
        commit_dirty_worktree=AsyncMock(
            side_effect=ProviderManagedKeyParkedError("PROVIDER_MANAGED_KEY_UNAVAILABLE")
        ),
        dirty_results=[None],
    )
    state = MonitorState()

    with pytest.raises(ProviderManagedKeyParkedError) as exc_info:
        await _run_ci_fix(runner, workspace_id=workspace_id, tmp_path=tmp_path, state=state)

    salvage.assert_not_awaited()
    rollback.assert_not_awaited()
    assert state.threads_addressed_ids[PROVIDER_PARK_STRANDED_EDITS_STATE_KEY] == _POST_SINK_HEAD
    assert exc_info.value.details is not None
    assert exc_info.value.details["phase"] == "ci_repair_commit_sink"
