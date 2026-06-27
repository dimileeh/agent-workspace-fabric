"""Pre-push validation fix-pass recovered-agent retry guard tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorMirrorHooksPathRepairFailedError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_runner_coverage_edges_parts.test_pr_monitor_runner_coverage_edges_part_020 import (
    _write_failed_validation_result,
    _write_worktree_with_mirror,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        from awf.db.session import make_session_factory

        yield make_session_factory(engine)


@pytest.mark.unit
@pytest.mark.parametrize(
    "guard_exc",
    [
        ProviderRecoveryRetryError(),
        _MonitorAgentRuntimeOwnershipRepairFailedError("AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"),
        _MonitorMirrorHooksPathRepairFailedError(),
    ],
)
async def test_pre_push_validation_fix_pass_propagates_recovered_agent_pre_retry_guard_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard_exc: Exception,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    _write_worktree_with_mirror(tmp_path, workspace_id)

    cmd = FakeCommandRunner()
    fix_start_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )

    rollback_calls: list[str] = []

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return True

    async def _run_monitor_agent_with_service_recovery(**kwargs: object) -> object:
        del kwargs
        raise guard_exc

    async def _rollback_failed_fix_pass(_runner: object, **kwargs: object) -> str | None:
        del _runner
        rollback_calls.append(str(kwargs["reason"]))
        return None

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _run_monitor_agent_with_service_recovery,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass.repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.pre_push_validation._rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    from awf.runtime.pr_monitor_runner.pre_push_validation import _run_pre_push_validation_fix_pass

    with pytest.raises(type(guard_exc)):
        await _run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="awf/ws_test",
            remote_url=None,
            state=None,
            validation_result=_write_failed_validation_result(tmp_path),
            pass_number=1,
            total_passes=1,
            validation_commands=("ruff check",),
        )

    assert rollback_calls == []
