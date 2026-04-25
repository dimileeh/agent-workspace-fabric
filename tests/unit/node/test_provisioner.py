"""Provisioner tests — real GitManager against a throwaway git repo + SQLite DB.

We exercise the full provisioner flow rather than mocking git, because the whole
point is the integration between state transitions and filesystem operations.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner import Provisioner, ProvisionerConfig


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # File-based SQLite so multiple sessions see the same state (the provisioner
    # opens several short sessions across the flow).
    db_path = tmp_path / "awf-test.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def git_manager(tmp_path: Path) -> GitManager:
    return GitManager(tmp_path / "awf-work")


@pytest.fixture
def provisioner(
    session_factory: async_sessionmaker[AsyncSession], git_manager: GitManager
) -> Provisioner:
    return Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )


class TestSuccess:
    @pytest.mark.unit
    async def test_transitions_to_ready_only_after_stack_launch_succeeds(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []
                self.statuses_seen: list[str] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                async with session_factory() as s:
                    persisted = await WorkspaceRepository(s).get(request.workspace_id)
                    assert persisted is not None
                    self.statuses_seen.append(persisted.status)
                return object()

        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert len(launcher.requests) == 1
        request = launcher.requests[0]
        assert request.workspace_id == ws_id
        assert request.layout.worktree_path == git_manager.work_dir / "worktrees" / ws_id
        assert request.layout.branch_name == f"awf/{ws_id}"
        assert request.profile.name == "generic"
        assert launcher.statuses_seen == [WorkspaceStatus.provisioning.value]

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.compose_project_name == f"awf_{ws_id}"

    @pytest.mark.unit
    async def test_transitions_requested_to_ready(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.node_id == "test-node-01"
            assert reloaded.branch_name == f"awf/{ws_id}"
            assert reloaded.base_commit is not None
            assert len(reloaded.base_commit) == 40  # SHA1 hex
            assert reloaded.compose_project_name == f"awf_{ws_id}"

    @pytest.mark.unit
    async def test_records_state_transition_events(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            transitions = [(e.old_state, e.new_state) for e in reloaded.events]
            assert (None, "requested") in transitions
            assert ("requested", "provisioning") in transitions
            assert ("provisioning", "ready") in transitions


class TestFailureHandling:
    @pytest.mark.unit
    async def test_stack_startup_failure_marks_workspace_failed_with_actionable_message(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _FailingStackLauncher:
            async def launch(self, request: Any) -> object:
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="pull access denied for awf-agent-runtime:test",
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingStackLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            assert reloaded.failure_message is not None
            assert "docker compose up failed" in reloaded.failure_message
            assert "pull access denied for awf-agent-runtime:test" in reloaded.failure_message

    @pytest.mark.unit
    async def test_missing_base_branch_marks_workspace_failed(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="nonexistent",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(GitOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message is not None


class TestIdempotency:
    @pytest.mark.unit
    async def test_skips_already_provisioning_workspace(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        # First provisioning drives it to ready.
        await provisioner.provision(ws_id)

        # Second call should be a no-op — status is already ready.
        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
