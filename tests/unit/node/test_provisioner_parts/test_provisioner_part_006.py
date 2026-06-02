"""Provisioner idempotency and failure edge tests split from part 002."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import EgressAuditRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.egress_policy import LocalEgressPolicyError
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.profiles.models import EgressMode
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


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


@pytest.fixture
def provisioner(
    session_factory: async_sessionmaker[AsyncSession], git_manager: GitManager
) -> Provisioner:
    return Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )


class TestIdempotencyPart006:
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

        await provisioner.provision(ws_id)
        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value


class TestFailureHandlingEdgesPart006:
    async def test_local_egress_policy_error_marks_workspace_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _EgressFailingLauncher:
            async def launch(self, _request: Any) -> object:
                raise LocalEgressPolicyError(
                    reason_code="LOCAL_EGRESS_MODE_UNSUPPORTED",
                    mode=EgressMode.restricted,
                    message="local backend cannot enforce this mode",
                    details={"network_posture": "restricted"},
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_EgressFailingLauncher(),  # type: ignore[arg-type]
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
                resolved_profile={"name": "restricted-local"},
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(LocalEgressPolicyError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            audit = await EgressAuditRepository(s).get_latest_for_workspace(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "policy_failure"
            assert reloaded.failure_message is not None
            assert "LOCAL_EGRESS_MODE_UNSUPPORTED" in reloaded.failure_message
            assert audit is None
            failed_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            ]
            assert failed_events[-1].reason_code == "LOCAL_EGRESS_MODE_UNSUPPORTED"

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

    async def test_unexpected_provisioning_failure_marks_workspace_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _ExplodingStackLauncher:
            async def launch(self, request: Any) -> object:
                del request
                raise RuntimeError("template workspace.base.yml.j2 was not found")

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_ExplodingStackLauncher(),
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

        with pytest.raises(RuntimeError, match="workspace.base.yml.j2"):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message is not None
            assert "unexpected provisioning failure" in reloaded.failure_message
            assert "workspace.base.yml.j2" in reloaded.failure_message

    async def test_unexpected_launch_failure_marks_failed_when_terminal_cleanup_check_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _ExplodingStackLauncher:
            async def launch(self, request: Any) -> object:
                del request
                raise RuntimeError("template workspace.base.yml.j2 was not found")

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_ExplodingStackLauncher(),
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

        async def _raise_launch_lost_cleanup(wid: str) -> bool:
            assert wid == ws_id
            raise RuntimeError("terminal cleanup check lost its DB session")

        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup",
            _raise_launch_lost_cleanup,
        )

        with pytest.raises(RuntimeError, match="workspace.base.yml.j2"):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message is not None
            assert "unexpected provisioning failure" in reloaded.failure_message
            assert "workspace.base.yml.j2" in reloaded.failure_message
            assert reloaded.compose_project_name == f"awf_{ws_id}"

    async def test_pre_launch_unexpected_failure_does_not_claim_runtime_ports(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                raise AssertionError("pre-launch failure should not call launch")

        class _PreLaunchFailingProvisioner(Provisioner):
            async def _materialize_companions(
                self,
                *,
                workspace_id: str,
                companions: tuple[WorkspaceCompanionSpec, ...],
                default_base_branch: str,
            ) -> tuple[MaterializedCompanionService, ...]:
                del workspace_id, companions, default_base_branch
                raise RuntimeError("companion worktree materialization exploded")

        launcher = _RecordingStackLauncher()
        provisioner = _PreLaunchFailingProvisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="companion pre-launch failure",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                task_policy={
                    "companions": [
                        {
                            "name": "web",
                            "repo_url": str(origin_repo),
                            "ports": [[80, 18080]],
                        }
                    ]
                },
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(RuntimeError, match="companion worktree materialization exploded"):
            await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            reloaded = await repo.get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.compose_project_name is None

            conflicts = await repo.find_host_port_conflicts(
                host_ports=[18080],
                excluding_workspace_id=None,
            )
            assert conflicts == []
