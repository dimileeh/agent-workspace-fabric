"""A PR AWF opens itself starts the disposition gate from a settled baseline (#1018).

The gate's one-time legacy baseline grandfathers unmarked AWF resolutions that
predate reply-before-resolve. A PR the executor opens now has no such history,
so its handoff settles the baseline empty: the first audit grandfathers nothing.
A reused PR (already persisted before this handoff) may carry pre-rollout
history, so it keeps the one-time baseline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.control.executor.execution_pr_handoff import persist_pr_and_handoff
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.monitor_state_keys import _REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"
_PR_URL = "https://github.com/dimileeh/aira-agent/pull/123"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _StubMonitor:
    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        del compose_project, compose_file
        self.runs.append(workspace_id)


def _executor(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, monitor: _StubMonitor
) -> WorkspaceExecutor:
    fake = FakeCommandRunner()
    return WorkspaceExecutor(
        session_factory=factory,
        runner=fake,
        compose=ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE),
        validation=ValidationRunner(runner=fake, artifacts_dir=tmp_path / "artifacts"),
        pr_creator=PullRequestCreator(fake),
        config=ExecutorConfig(
            worktrees_root=tmp_path / "work" / "worktrees",
            compose_projects_root=tmp_path / "work" / "compose",
        ),
        pr_monitor=monitor,
    )


async def _seed_pushing(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_url: str | None,
    monitor_state: dict[str, str] | None = None,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        for status in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(ws, to=status, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.pr_url = pr_url
        ws.monitor_threads_addressed = monitor_state
        await s.commit()
        return ws.id


async def _handoff(executor: WorkspaceExecutor, workspace_id: str) -> None:
    await persist_pr_and_handoff(
        executor,
        workspace_id=workspace_id,
        pr=SimpleNamespace(
            url=_PR_URL, head_sha="deadbeef01", branch=f"awf/{workspace_id}", open_metadata=None
        ),
        adapter=None,
        profile=None,
        defaults=None,
        successful_validation_run_id=None,
        compose_project="proj",
        compose_file=Path("/tmp/compose.yml"),
    )


async def _monitor_state(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> dict[str, str]:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        return dict(ws.monitor_threads_addressed or {})


async def test_newly_opened_pr_hands_off_with_a_settled_disposition_baseline(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    monitor = _StubMonitor()
    workspace_id = await _seed_pushing(factory, pr_url=None)

    await _handoff(_executor(factory, tmp_path, monitor), workspace_id)

    assert monitor.runs == [workspace_id]
    state = await _monitor_state(factory, workspace_id)
    assert state[_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY] == "[]"


async def test_reused_pr_keeps_the_one_time_baseline_for_pre_rollout_history(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    monitor = _StubMonitor()
    workspace_id = await _seed_pushing(
        factory, pr_url=_PR_URL, monitor_state={"PRRT_test_old": "fix_committed"}
    )

    await _handoff(_executor(factory, tmp_path, monitor), workspace_id)

    assert monitor.runs == [workspace_id]
    assert await _monitor_state(factory, workspace_id) == {"PRRT_test_old": "fix_committed"}
