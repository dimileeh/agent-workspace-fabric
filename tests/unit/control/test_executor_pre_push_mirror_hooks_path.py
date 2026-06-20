"""Executor mirror hooks path repair before PR push."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor import execution_flow as execution_flow_module
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_creator import PullRequestCreator, PullRequestResult
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine
from tests.unit.control.executor_paths import _test_worktrees_root
from tests.unit.control.test_executor_parts.test_executor_part_001 import (
    _queue_validation_head,
    _seed_ready_workspace,
)

_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "compose" / "workspace.base.yml.j2"


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
            worktrees_root=_test_worktrees_root(factory),
            compose_projects_root=tmp_path / "work" / "compose",
            default_models={
                AgentRuntime.codex: "gpt-5",
                AgentRuntime.claude_code: "sonnet",
                AgentRuntime.gemini: "gemini-2.5-pro",
            },
        ),
    )


@pytest.mark.unit
async def test_execute_repairs_mirror_hooks_path_after_validation_before_pr_push(
    executor: WorkspaceExecutor,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mirror hooks repair failure after validation must stop before push."""
    ws_id = await _seed_ready_workspace(factory)
    mirror_path = tmp_path / "mirror.git"
    repair_calls: list[Path] = []
    push_attempts: list[str] = []

    async def _repair_mirror_hooks_path(path: Path) -> bool:
        repair_calls.append(path)
        if len(repair_calls) == 6:
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=128,
                stdout="",
                stderr="could not lock config file\n",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return True

    async def _push_and_open_pr(*_args: object, **kwargs: object) -> PullRequestResult:
        push_attempts.append(str(kwargs["workspace_id"]))
        return PullRequestResult(
            url="https://github.com/dimileeh/aira-agent/pull/777",
            branch=f"awf/{ws_id}",
            head_sha="f" * 40,
        )

    monkeypatch.setattr(
        execution_flow_module, "mirror_path_for_worktree", lambda _path: mirror_path
    )
    monkeypatch.setattr(
        execution_flow_module, "repair_mirror_hooks_path", _repair_mirror_hooks_path
    )
    monkeypatch.setattr(
        execution_flow_module._pr_open_step,
        "push_and_open_pr",
        _push_and_open_pr,
    )

    fake.queue_result(returncode=0)  # adapter
    fake.queue_result(returncode=0, stdout=f"awf/{ws_id}\n")  # current branch
    fake.queue_result(returncode=0)  # git add -A
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # diff --cached
    fake.queue_result(returncode=0)  # git commit
    fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
    fake.queue_result(returncode=0)  # merge-base is-ancestor ok
    _queue_validation_head(fake)
    fake.queue_result(returncode=0, stdout="tests ok")  # validation command
    fake.queue_result(returncode=0, stdout="src/fix.py\n")  # committed paths
    fake.queue_result(returncode=0, stdout="M\0src/fix.py\0")  # protected-file diff

    await executor.execute(ws_id)

    assert repair_calls == [mirror_path] * 6
    assert push_attempts == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert ws.failure_message == "could not repair poisoned mirror hooks path before PR push"
        assert ws.events[-1].reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
