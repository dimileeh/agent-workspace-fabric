"""Regression test for cleanup-won race on failed compose launch.

When an operator stop/destroy records workspace.terminal_runtime_released
after _recheck_before_launch() but before Docker actually starts, the compose
launch can still raise ComposeOperationError (for example a health-check
timeout).  The _launch_lost_to_terminal_cleanup() guard was only called
after a successful launch, so the compose-failure path skipped orphan
cleanup/revocation, leaving a failed workspace with an effective release
event; host-port admission then ignored ports that the retained failed
stack may still be binding.

The fix adds the terminal-cleanup check to the ComposeOperationError
handler so that orphan containers are stopped and the release event is
revoked even when the launch fails.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.postgres import postgres_test_engine


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
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def git_manager(tmp_path: Path) -> GitManager:
    return GitManager(tmp_path / "awf-work")


class _FailingComposeLauncher:
    """Stack launcher that always raises ComposeOperationError."""

    async def launch(self, request: Any) -> object:
        on_started = getattr(request, "on_compose_up_started", None)
        if on_started is not None:
            await on_started()
        raise ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="dependency failed to start: container backend is unhealthy",
            reason_code="COMPOSE_COMMAND_FAILED",
        )


class TestComposeFailureTerminalCleanupRace:
    """Regression: _launch_lost_to_terminal_cleanup must run on compose failure."""

    @pytest.mark.asyncio
    async def test_compose_failure_checks_terminal_cleanup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="cleanup-race-test",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        cleanup_called = False

        async def _fake_launch_lost_cleanup(wid: str) -> bool:
            nonlocal cleanup_called
            cleanup_called = True
            assert wid == ws_id
            return False

        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup",
            _fake_launch_lost_cleanup,
        )

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        assert cleanup_called, "_launch_lost_to_terminal_cleanup must be called when compose fails"

    @pytest.mark.asyncio
    async def test_compose_failure_returns_early_when_terminal_cleanup_won(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="cleanup-won-test",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        mark_failed_called = False

        original_mark_failed = provisioner._mark_failed

        async def _tracking_mark_failed(**kwargs: Any) -> None:
            nonlocal mark_failed_called
            mark_failed_called = True
            await original_mark_failed(**kwargs)

        monkeypatch.setattr(provisioner, "_mark_failed", _tracking_mark_failed)

        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup",
            AsyncMock(return_value=True),
        )

        await provisioner.provision(ws_id)

        assert not mark_failed_called, (
            "_mark_failed should not be called from ComposeOperationError "
            "when _launch_lost_to_terminal_cleanup returns True (workspace "
            "already in terminal state)"
        )

    @pytest.mark.asyncio
    async def test_compose_failure_proceeds_when_no_terminal_cleanup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="no-terminal-cleanup-test",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup",
            AsyncMock(return_value=False),
        )

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"

    @pytest.mark.asyncio
    async def test_compose_failure_marks_failed_when_terminal_cleanup_check_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="cleanup-check-raises-test",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await s.commit()
            ws_id = ws.id

        async def _raise_launch_lost_cleanup(wid: str) -> bool:
            assert wid == ws_id
            raise RuntimeError("terminal cleanup check lost its DB session")

        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup",
            _raise_launch_lost_cleanup,
        )

        with pytest.raises(ComposeOperationError) as raised:
            await provisioner.provision(ws_id)

        assert raised.value.reason_code == "COMPOSE_COMMAND_FAILED"
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            assert reloaded.compose_project_name == f"awf_{ws_id}"


class TestRecheckBeforeLaunchFailure:
    """Regression: a pre-launch recheck failure must not leave a port blocker."""

    @pytest.mark.asyncio
    async def test_recheck_exception_clears_prepublished_compose_project(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        from awf.db.repositories.base import has_terminal_runtime_released_event

        class _LaunchShouldNotRun:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                raise AssertionError("recheck failure should abort before launch")

        class _RecheckFailingProvisioner(Provisioner):
            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                async with self._session_factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    assert ws.status == WorkspaceStatus.provisioning.value
                    assert ws.compose_project_name == f"awf_{workspace_id}"
                raise RuntimeError("terminal launch guard recheck lost its DB session")

        launcher = _LaunchShouldNotRun()
        provisioner = _RecheckFailingProvisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="recheck-before-launch failure",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                resolved_profile={
                    "name": "recheck-before-launch-profile",
                    "services": [
                        {
                            "name": "db",
                            "image": "postgres:16",
                            "ports": [[80, 19080]],
                        }
                    ],
                },
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            reloaded = await repo.get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.compose_project_name is None
            assert await has_terminal_runtime_released_event(s, ws_id) is False
            conflicts = await repo.find_host_port_conflicts(
                host_ports=[19080],
                excluding_workspace_id=None,
                node_id="test-node-01",
            )
            assert conflicts == []
