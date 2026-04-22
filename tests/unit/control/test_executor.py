"""Executor tests with FakeCommandRunner + in-memory SQLite.

Each test drives one workspace through the full pipeline with canned
subprocess output. The single runner handles all compose/adapter/pr calls
since each call is distinguishable by its argv.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 - populates adapter registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import ExecutorConfig, WorkspaceExecutor
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.validation import ValidationRunner

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


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


async def _seed_ready_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "codex",
    test_commands: list[str] | None = None,
    requires_database: bool = False,
) -> str:
    """Insert a workspace already in the ``ready`` state for the executor to pick up."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-agent.git",
            branch_base="development",
            task_title="trivial",
            task_prompt="Add a docstring.",
            agent=agent,
            test_commands=test_commands or ["pytest -q"],
            requires_database=requires_database,
        )
        # Walk through the transitions: requested → provisioning → ready.
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="X")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="X")
        await s.commit()
        return ws.id


class TestHappyPath:
    @pytest.mark.unit
    async def test_drives_ready_to_completed_and_records_pr_url(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)

        # Queue results for the full sequence:
        # (1) adapter.run, (2) git add -A, (3) git diff --cached --name-only,
        # (4) git commit, (5) git rev-list --count base..HEAD,
        # (6) git merge-base --is-ancestor base HEAD,
        # (7) validation (one test cmd), (8) git push, (9) gh pr create.
        fake.queue_result(returncode=0, stdout="codex finished")  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="CHANGELOG.md\n")  # cached diff (non-empty)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0, stdout="tests ok")  # validation cmd
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/123\n",
        )  # gh pr create

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/123"

    @pytest.mark.unit
    async def test_records_all_expected_transitions(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        # Same 8-step sequence as the happy-path test.
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0)  # validation
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            transitions = [(e.old_state, e.new_state) for e in ws.events]
            assert ("ready", "running") in transitions
            assert ("running", "validating") in transitions
            assert ("validating", "pushing") in transitions
            assert ("pushing", "completed") in transitions


class TestFailurePaths:
    @pytest.mark.unit
    async def test_agent_failure_marks_workspace_failed_and_stops_pipeline(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        # Agent run returns non-zero → AgentRunError.
        fake.queue_result(returncode=2, stderr="codex: auth failed")

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            # Validation + PR never ran.
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_validation_failure_marks_failed_with_reason(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter ok
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached (non-empty)
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=1, stderr="pytest: 5 failed")  # validation fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "validation_failure"

    @pytest.mark.unit
    async def test_push_failure_marks_failed_with_infrastructure_reason(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter ok
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0)  # validation ok
        fake.queue_result(returncode=128, stderr="remote: perm denied")  # push fails

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"

    @pytest.mark.unit
    async def test_agent_makes_no_changes_marks_failed(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter returns "ok" but changed nothing
        fake.queue_result(returncode=0)  # git add produces nothing
        fake.queue_result(returncode=0, stdout="")  # diff --cached is empty (no staged)
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list count is 0 — no progress

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "no commits" in (ws.failure_message or "") or "without producing" in (
                ws.failure_message or ""
            )

    @pytest.mark.unit
    async def test_orphan_history_is_recovered_and_pipeline_continues(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Agents sometimes sever git history (e.g. `git checkout --orphan` +
        # fresh commit) — the branch has commits but no shared ancestor
        # with the base. `rev-list --count base..HEAD` can't detect this
        # (count is HIGH — every HEAD commit is "new" when there's no merge
        # base), so the previous no-changes check lets it through, and
        # `gh pr create` dies with GraphQL "no history in common".
        #
        # Recovery: `git reset --soft <base>` keeps the index at the
        # orphan's tree while moving HEAD to base. A fresh commit then
        # squashes the entire orphan chain into one commit on top of base.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(returncode=0)  # git reset --soft <base>
        fake.queue_result(returncode=0)  # git commit (re-anchor)
        fake.queue_result(returncode=0)  # merge-base is-ancestor: OK after recovery
        fake.queue_result(returncode=0, stdout="recovery tests ok")  # validation cmd
        fake.queue_result(returncode=0)  # git push
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/456\n",
        )  # gh pr create

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_url == "https://github.com/dimileeh/aira-agent/pull/456"
        # reset + commit + verify show up in the call sequence in order.
        reset_call = next(c for c in fake.calls if "reset" in c.args and "--soft" in c.args)
        assert reset_call.args[-1] == "a" * 40  # base_commit
        # Two `merge-base --is-ancestor` calls (pre and post recovery).
        ancestor_calls = [c for c in fake.calls if "merge-base" in c.args]
        assert len(ancestor_calls) == 2

    @pytest.mark.unit
    async def test_orphan_history_fails_loudly_if_recovery_fails(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # If the post-recovery ancestry check still fails (pathological
        # case — e.g. base_commit not reachable), mark failed with a clear
        # message so the operator knows what happened and doesn't chase a
        # ``gh pr create`` GraphQL error.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list count
        fake.queue_result(returncode=1, stderr="")  # merge-base is-ancestor: FAIL
        fake.queue_result(
            returncode=128, stderr="fatal: unknown revision"
        )  # git reset --soft: FAIL

        await executor.execute(ws_id)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert "history" in (ws.failure_message or "").lower()
            assert ws.pr_url is None


class TestIdempotency:
    @pytest.mark.unit
    async def test_refuses_to_run_on_non_ready_workspace(
        self,
        executor: WorkspaceExecutor,
        fake: FakeCommandRunner,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Seed then drive to completed via a first execute call.
        ws_id = await _seed_ready_workspace(factory)
        fake.queue_result(returncode=0)  # adapter
        fake.queue_result(returncode=0)  # git add
        fake.queue_result(returncode=0, stdout="f\n")  # diff --cached
        fake.queue_result(returncode=0)  # git commit
        fake.queue_result(returncode=0, stdout="1\n")  # rev-list count
        fake.queue_result(returncode=0)  # merge-base --is-ancestor ok
        fake.queue_result(returncode=0)  # validation
        fake.queue_result(returncode=0)  # push
        fake.queue_result(returncode=0, stdout="https://github.com/a/b/pull/1")  # gh pr create
        await executor.execute(ws_id)

        # Second call must be a no-op — status is completed.
        calls_before = len(fake.calls)
        await executor.execute(ws_id)
        assert len(fake.calls) == calls_before

    @pytest.mark.unit
    async def test_unknown_workspace_is_silent_noop(
        self, executor: WorkspaceExecutor, fake: FakeCommandRunner
    ) -> None:
        await executor.execute("ws_never_existed")
        assert fake.calls == []
