"""Regression coverage for setup cleanup missing-HEAD recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import execution_flow as execution_flow_module
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.test_executor_parts.test_executor_part_001 import _seed_ready_workspace

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        session_factory = make_session_factory(engine)
        session_factory._awf_test_worktrees_root = tmp_path / "work" / "worktrees"  # type: ignore[attr-defined]
        yield session_factory


@pytest.fixture
def fake() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> WorkspaceExecutor:
    compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    validation = ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts")
    pr = PullRequestCreator(fake)
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=compose,
        validation=validation,
        pr_creator=pr,
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


@pytest.mark.unit
async def test_setup_cleanup_failure_recovers_missing_head_before_outer_failure(
    executor: WorkspaceExecutor,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed_ready_workspace(factory)

    async def _run_phases(*, phase_names: tuple[str, ...], **_kwargs: object) -> object:
        if phase_names == ("setup", "pre_agent"):
            raise ComposeExecCleanupError(
                invocation_id="setup-cleanup",
                source="agent",
                label="setup",
                message="cleanup timed out",
            )
        raise AssertionError("validation should not run after setup cleanup failure")

    verify_head = AsyncMock(return_value=False)
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr(executor._validation, "run_profile_phases", _run_phases)
    monkeypatch.setattr(execution_flow_module, "verify_head_object_exists", verify_head)
    monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)

    await executor.execute(ws_id)

    verify_head.assert_awaited_once()
    recover.assert_awaited_once()
    assert recover.await_args.kwargs["stage"] == "profile_setup_cleanup_failure"
    assert recover.await_args.kwargs["mark_failed_on_failure"] is False
    assert recover.await_args.kwargs["branch_name"] == f"awf/{ws_id}"

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
