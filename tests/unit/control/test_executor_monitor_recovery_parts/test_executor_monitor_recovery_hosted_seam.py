"""Hosted runtime execution seam — PR monitor resume handoff.

When an ``AgentRuntimeExecutor`` is injected into the ``WorkspaceExecutor``,
``resume_pr_monitor_handoff`` must skip the docker compose restart block
(there is no compose project to restart in hosted mode) and still build the
monitor + return a ``ResumeHandoff`` whose ``monitor.run`` drives the loop
via the injected executor. Local Core (executor is None) keeps the exact
compose restart behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populates registry
from awf.adapters.runtime_executor import (
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
)
from awf.common.commands import FakeCommandRunner
from awf.control.executor import (
    ExecutorConfig,
    WorkspaceExecutor,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine

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


async def _seed_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:x/y.git",
            branch_base="development",
            task_title="monitor-resume-hosted",
            task_prompt="p",
            agent="codex",
            test_commands=["pytest -q"],
            requires_database=False,
            auto_merge=True,
        )
        ws.task_kind = "feature_branch_pr"
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = "awf/x"
        ws.remote_push_branch = "awf/x"
        ws.base_commit = "a" * 40
        ws.compose_project_name = "awf_x"
        ws.compose_file_path = "/tmp/awf/x/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = "https://github.com/x/y/pull/42"
        ws.pr_number = 42
        ws.monitor_last_commit_sha = "b" * 40
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class _RecordingCompose:
    """Records ``ensure_project_up`` calls so we can assert restart skipped."""

    def __init__(self) -> None:
        self.ensure_project_up_calls: list[str] = []

    async def ensure_project_up(
        self,
        *,
        project_name: str,
        compose_file: Path,
        workspace_id: str,
        wait: bool = True,
        compose_up_timeout_seconds: int = 300,
        force_recreate: bool = False,
        services: tuple[str, ...] = (),
    ) -> None:
        del project_name, compose_file, wait, compose_up_timeout_seconds
        del force_recreate, services
        self.ensure_project_up_calls.append(workspace_id)


class _RecordingExecutor:
    """Hosted executor stub that records the execute() call."""

    def __init__(self) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        return AgentRuntimeExecResult(returncode=0, stdout="hosted ok", stderr="")


class _RecordingMonitor:
    """Monitor stub that records ``run`` invocation."""

    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> None:
        self.run_calls.append(dict(kwargs))


def _make_executor(
    *,
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    compose: Any,
    pr_monitor_factory: Any,
    agent_runtime_executor: Any = None,
) -> WorkspaceExecutor:
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
            max_validation_fix_passes=5,
        ),
        pr_monitor_factory=pr_monitor_factory,
        agent_runtime_executor=agent_runtime_executor,
    )


class TestResumeHandoffHostedSeam:
    @pytest.mark.unit
    async def test_injected_executor_skips_compose_restart(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        compose = _RecordingCompose()
        executor = _RecordingExecutor()
        monitor = _RecordingMonitor()
        # Use a ComposeManager-compatible compose for the constructor (the
        # recording compose is only used to assert restart is skipped).
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=executor,
        )
        # Swap in the recording compose so we can assert ensure_project_up
        # was NOT called on the hosted path.
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        assert compose.ensure_project_up_calls == []
        assert len(monitor.run_calls) == 1
        assert monitor.run_calls[0]["workspace_id"] == ws_id

    @pytest.mark.unit
    async def test_local_path_without_executor_still_restarts_compose(
        self,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_pr(factory)
        compose = _RecordingCompose()
        monitor = _RecordingMonitor()
        real_compose = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
        executor_obj = _make_executor(
            fake=fake,
            factory=factory,
            tmp_path=tmp_path,
            compose=real_compose,
            pr_monitor_factory=lambda *_args, **_kwargs: monitor,
            agent_runtime_executor=None,
        )
        executor_obj._compose = compose  # type: ignore[method-assign]

        await executor_obj.resume_pr_monitor(ws_id)

        # Local path still restarts compose exactly once.
        assert compose.ensure_project_up_calls == [ws_id]
        assert len(monitor.run_calls) == 1
