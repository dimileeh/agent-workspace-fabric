"""Provisioner ready-state and persisted-profile tests."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    EgressAuditRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeProjectPaths
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


@pytest.fixture
def provisioner(
    session_factory: async_sessionmaker[AsyncSession], git_manager: GitManager
) -> Provisioner:
    return Provisioner(
        session_factory=session_factory,
        git=git_manager,
        config=ProvisionerConfig(node_id="test-node-01"),
    )


class TestReadyState:
    @pytest.mark.unit
    async def test_uses_persisted_resolved_profile_without_rewriting_it(
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
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

        resolved_profile = {
            "name": "persisted-profile",
            "source": "repo:.awf/workspace.yml",
            "phases": {"validate": [{"command": "pytest -q"}]},
        }
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
                test_commands=["ruff check"],
                profile_ref="python",
                requested_profile={"name": ""},
                resolved_profile=resolved_profile,
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert len(launcher.requests) == 1
        assert launcher.requests[0].profile.name == "persisted-profile"
        assert [c.command for c in launcher.requests[0].profile.phases.validate_commands] == [
            "pytest -q"
        ]
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.profile_ref == "python"
            assert reloaded.resolved_profile == resolved_profile

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
    async def test_egress_audit_records_workspace_task_attempt(
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
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=f"provisioner-audit:{ws.id}",
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=ws,
            )
            await s.commit()
            ws_id = ws.id
            attempt_id = attempt.id
        await provisioner.provision(ws_id)
        async with session_factory() as s:
            audit = await EgressAuditRepository(s).get_latest_for_workspace(ws_id)
            assert audit is not None
            assert audit.attempt_id == attempt_id

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

    @pytest.mark.unit
    async def test_provisioner_updates_auto_profile_dind_reservation(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        (origin_repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        _git(["add", "docker-compose.yml"], origin_repo)
        _git(["commit", "-q", "-m", "add compose profile"], origin_repo)
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                profile_ref="auto",
                test_commands=[],
            )
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=f"provisioner-dind:{ws.id}",
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            attempt = await TaskAttemptRepository(s).create_for_workspace(
                task=task,
                workspace=ws,
            )
            await ResourceReservationRepository(s).create(
                workspace_id=ws.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=3.0,
                steady_memory_gb=10.0,
                peak_cpu=6.0,
                peak_memory_gb=16.0,
                disk_mb=None,
                dind_slots=0,
                phase="workspace_lifecycle",
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.resolved_profile is not None
            assert reloaded.resolved_profile["docker"]["mode"] == "dind"
            reservation = await ResourceReservationRepository(s).active_for_workspace(ws_id)
            assert reservation is not None
            assert reservation.dind_slots == 1
            assert reservation.node_id == "test-node-01"
