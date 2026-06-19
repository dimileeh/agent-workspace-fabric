"""Additional protected-scope PR monitor runner edge coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import remote_ops as pr_remote_ops
from awf.runtime.pr_monitor_runner import remote_repair_protected as pr_remote_repair_protected
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
)
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


_PROTECTED_WORKFLOW_BLOCKED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()


@pytest.mark.unit
async def test_protected_scope_diff_unavailable_push_result_uses_block_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _block(**_kwargs: object) -> pr_remote_ops._ProtectedScopePushBlock:  # noqa: SLF001
        return pr_remote_ops._ProtectedScopePushBlock(  # noqa: SLF001
            message="diff unavailable",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_block", _block)

    result = await runner._protected_scope_diff_unavailable_push_result(
        workspace_id="ws_delta",
        remote_branch="awf/ws_delta",
        exc=ProtectedScopeDiffError("no diff"),
    )

    assert result.failed is True
    assert result.protected_scope_diff_unavailable is True
    assert result.stderr == "diff unavailable"


@pytest.mark.unit
async def test_protected_scope_repair_raises_on_ownership_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # cat-file HEAD:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_BLOCKED)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, reason, event_name, reason_code
        return False

    monkeypatch.setattr(
        pr_remote_repair_protected,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError) as exc_info:
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    assert exc_info.value.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
