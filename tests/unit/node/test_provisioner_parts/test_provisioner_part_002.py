"""Provisioner tests — real GitManager against a throwaway git repo + PostgreSQL DB.

We exercise the full provisioner flow rather than mocking git, because the whole
point is the integration between state transitions and filesystem operations.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError, ComposeProjectPaths
from awf.node.git_manager import GitManager, WorktreeLayout
from awf.node.provisioner import (
    Provisioner,
    ProvisionerConfig,
)
from awf.profiles.models import ProfileSecret, WorkspaceProfile
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


async def _force_destroy_provisioning_workspace(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.provisioning.value
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_DESTROY")
        await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="TEST_DESTROY")
        await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="TEST_DESTROY")
        await s.commit()


async def _force_cancel_provisioning_workspace(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.provisioning.value
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL")
        await s.commit()


async def _signal_compose_up_started(request: Any) -> None:
    on_started = getattr(request, "on_compose_up_started", None)
    if on_started is not None:
        await on_started()


def _secret_profile() -> WorkspaceProfile:
    return WorkspaceProfile(
        name="provisioner-secret-edges",
        secrets=[
            ProfileSecret(
                name="api-token",
                kind="env",
                target="API_TOKEN",
                provider="env",
                ref="env/API_TOKEN",
            )
        ],
    )


class TestOperatorControlRaces:
    @pytest.mark.unit
    async def test_destroy_after_claim_skips_git_and_ready_transition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        class _RecordingGit:
            def __init__(self) -> None:
                self.add_worktree_calls: list[str] = []
                self.head_calls: list[str] = []

            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                self.add_worktree_calls.append(workspace_id)
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                self.head_calls.append(workspace_id)
                return "c" * 40

        class _DestroyAfterClaimProvisioner(Provisioner):
            async def _load_and_claim(self, session: AsyncSession, workspace_id: str) -> Any:
                ws = await super()._load_and_claim(session, workspace_id)
                assert ws is not None
                await _force_destroy_provisioning_workspace(session_factory, workspace_id)
                return ws

        fake_git = _RecordingGit()
        provisioner = _DestroyAfterClaimProvisioner(
            session_factory=session_factory,
            git=fake_git,  # type: ignore[arg-type]
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

        assert fake_git.add_worktree_calls == []
        assert fake_git.head_calls == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events
            assert skip_events[-1].reason_code == "PROVISIONER_STALE_STATUS"
            assert skip_events[-1].payload == {
                "action": "provision",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.destroyed.value,
            }

    @pytest.mark.unit
    async def test_destroy_after_git_add_skips_head_sha_and_stack_launch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        class _DestroyingGit:
            def __init__(self) -> None:
                self.add_worktree_calls: list[str] = []
                self.head_calls: list[str] = []

            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                self.add_worktree_calls.append(workspace_id)
                await _force_destroy_provisioning_workspace(session_factory, workspace_id)
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                self.head_calls.append(workspace_id)
                return "c" * 40

        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

        fake_git = _DestroyingGit()
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=fake_git,  # type: ignore[arg-type]
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

        assert fake_git.add_worktree_calls == [ws_id]
        assert fake_git.head_calls == []
        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events
            assert skip_events[-1].reason_code == "PROVISIONER_STALE_STATUS"
            assert skip_events[-1].payload == {
                "action": "provision",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.destroyed.value,
            }

    @pytest.mark.unit
    async def test_stack_failure_after_destroy_does_not_mark_workspace_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _DestroyingFailingStackLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                await _force_destroy_provisioning_workspace(session_factory, request.workspace_id)
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="workspace was already destroyed",
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_DestroyingFailingStackLauncher(),
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
            assert reloaded.status == WorkspaceStatus.destroyed.value
            assert reloaded.failure_reason is None
            assert reloaded.failure_message is None
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events
            assert skip_events[-1].reason_code == "PROVISIONER_MARK_FAILED_SKIPPED"
            assert skip_events[-1].payload == {
                "action": "mark_failed",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.destroyed.value,
            }

    @pytest.mark.unit
    async def test_destroy_after_secret_lease_issue_skips_stack_launch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        class _RecordingGit:
            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "d" * 40

        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

        class _DestroyingAfterIssueProvisioner(Provisioner):
            async def _issue_secret_leases(
                self,
                workspace_id: str,
                profile: WorkspaceProfile,
            ) -> None:
                await super()._issue_secret_leases(workspace_id, profile)
                await _force_destroy_provisioning_workspace(session_factory, workspace_id)

        launcher = _RecordingStackLauncher()
        provisioner = _DestroyingAfterIssueProvisioner(
            session_factory=session_factory,
            git=_RecordingGit(),  # type: ignore[arg-type]
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
                resolved_profile=_secret_profile().model_dump(mode="json"),
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert [lease.status for lease in leases] == ["issued"]
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events[-1].payload == {
                "action": "provision",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.destroyed.value,
            }

    @pytest.mark.unit
    async def test_destroy_after_stack_launch_skips_ready_transition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _DestroyingStackLauncher:
            async def launch(self, request: Any) -> object:
                await _force_destroy_provisioning_workspace(session_factory, request.workspace_id)
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_DestroyingStackLauncher(),
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

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value
            assert reloaded.node_id is None
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events[-1].payload == {
                "action": "provision",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.destroyed.value,
            }

    @pytest.mark.unit
    async def test_cancel_after_pre_launch_identity_commit_skips_stack_launch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        class _RecordingGit:
            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "d" * 40

        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

        class _CancellingBetweenRecheckAndLaunchProvisioner(Provisioner):
            _recheck_call_count: int = 0

            async def _recheck_status(
                self,
                workspace_id: str,
                *,
                expected: WorkspaceStatus,
                action: str,
                reason_code: str,
            ) -> bool:
                result = await super()._recheck_status(
                    workspace_id,
                    expected=expected,
                    action=action,
                    reason_code=reason_code,
                )
                self._recheck_call_count += 1
                if self._recheck_call_count == 4 and result and action == "provision":
                    await _force_destroy_provisioning_workspace(session_factory, workspace_id)
                return result

        launcher = _RecordingStackLauncher()
        provisioner = _CancellingBetweenRecheckAndLaunchProvisioner(
            session_factory=session_factory,
            git=_RecordingGit(),  # type: ignore[arg-type]
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
                resolved_profile=_secret_profile().model_dump(mode="json"),
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value
            assert reloaded.compose_project_name is None

    @pytest.mark.unit
    async def test_cancel_after_launch_guard_skips_terminal_runtime_released(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        from awf.db.repositories.base import (
            PROVISIONING_LAUNCHING_EVENT_TYPE,
            PROVISIONING_LAUNCHING_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )

        cancelled_between_guard_and_launch = False

        class _RecordingGit:
            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "e" * 40

        class _DelayedStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                nonlocal cancelled_between_guard_and_launch
                self.requests.append(request)
                assert cancelled_between_guard_and_launch, (
                    "cancel must happen before launch starts for this test to be meaningful"
                )
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_delayed"),
                    compose_file=Path("/tmp/awf-compose/ws_delayed/compose.yml"),
                )

        class _CancellingAfterLaunchGuardProvisioner(Provisioner):
            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                result = await super()._recheck_before_launch(workspace_id)
                if result:
                    await _force_cancel_provisioning_workspace(session_factory, workspace_id)
                    nonlocal cancelled_between_guard_and_launch
                    cancelled_between_guard_and_launch = True
                return result

        launcher = _DelayedStackLauncher()
        provisioner = _CancellingAfterLaunchGuardProvisioner(
            session_factory=session_factory,
            git=_RecordingGit(),
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
                resolved_profile=_secret_profile().model_dump(mode="json"),
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        assert len(launcher.requests) == 1
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.cancelled.value
            launching_events = [
                e
                for e in reloaded.events
                if e.event_type == PROVISIONING_LAUNCHING_EVENT_TYPE
                and e.reason_code == PROVISIONING_LAUNCHING_REASON_CODE
            ]
            assert len(launching_events) == 1
            terminal_release_events = [
                e
                for e in reloaded.events
                if e.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
                and e.reason_code == TERMINAL_RUNTIME_RELEASE_REASON_CODE
            ]
            assert len(terminal_release_events) == 0, (
                "terminal_runtime_released must not be recorded when "
                "provisioning_launching guard already committed"
            )

    @pytest.mark.unit
    async def test_force_destroy_after_launch_guard_stops_orphan_containers(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        from unittest.mock import patch

        from awf.db.repositories.base import (
            PROVISIONING_LAUNCHING_EVENT_TYPE,
            PROVISIONING_LAUNCHING_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )

        force_destroyed_between_guard_and_launch = False
        stopped_projects: list[str] = []

        class _RecordingGit:
            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "e" * 40

        class _DelayedStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                nonlocal force_destroyed_between_guard_and_launch
                self.requests.append(request)
                assert force_destroyed_between_guard_and_launch, (
                    "force-destroy must happen before launch starts for this test"
                )
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_force_destroy"),
                    compose_file=Path("/tmp/awf-compose/ws_force_destroy/compose.yml"),
                )

        async def _force_destroy_after_guard(
            sf: async_sessionmaker[AsyncSession], workspace_id: str
        ) -> None:
            async with sf() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.provisioning.value
                assert ws.compose_project_name is not None
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.cancelled,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.destroying,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.destroyed,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.add_event(
                    ws,
                    event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    payload={
                        "compose_project_name": ws.compose_project_name,
                        "workspace_status": WorkspaceStatus.destroyed.value,
                        "cleanup": {
                            "stack_stopped": False,
                            "source": "force_destroy_workspace",
                        },
                    },
                )
                await s.commit()

        class _ForceDestroyAfterLaunchGuardProvisioner(Provisioner):
            async def _recheck_status(
                self,
                workspace_id: str,
                *,
                expected: WorkspaceStatus,
                action: str,
                reason_code: str,
            ) -> bool:
                return await super()._recheck_status(
                    workspace_id,
                    expected=expected,
                    action=action,
                    reason_code=reason_code,
                )

            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                result = await super()._recheck_before_launch(workspace_id)
                if result:
                    await _force_destroy_after_guard(session_factory, workspace_id)
                    nonlocal force_destroyed_between_guard_and_launch
                    force_destroyed_between_guard_and_launch = True
                return result

        async def _mock_stop_project_containers(compose_project_name: str) -> None:
            if compose_project_name:
                stopped_projects.append(compose_project_name)

        launcher = _DelayedStackLauncher()
        provisioner = _ForceDestroyAfterLaunchGuardProvisioner(
            session_factory=session_factory,
            git=_RecordingGit(),
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
                resolved_profile=_secret_profile().model_dump(mode="json"),
            )
            await s.commit()
            ws_id = ws.id

        with patch(
            "awf.node.provisioner.stop_project_containers",
            new=_mock_stop_project_containers,
        ):
            await provisioner.provision(ws_id)

        assert len(launcher.requests) == 1, "stack should still have been launched"
        assert stopped_projects == [f"awf_{ws_id}"], (
            "orphan containers must be stopped after terminal cleanup won"
        )
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.destroyed.value, (
                "workspace must remain destroyed, not transition to ready"
            )
            launching_events = [
                e
                for e in reloaded.events
                if e.event_type == PROVISIONING_LAUNCHING_EVENT_TYPE
                and e.reason_code == PROVISIONING_LAUNCHING_REASON_CODE
            ]
            assert len(launching_events) == 1
            stale_skip_events = [
                e
                for e in reloaded.events
                if e.event_type == "workspace.stale_action_skipped"
                and e.reason_code == "TERMINAL_CLEANUP_WON_DURING_LAUNCH"
            ]
            assert len(stale_skip_events) == 1, (
                "provisioner must record stale-action-skip when terminal cleanup won"
            )
            assert stale_skip_events[0].payload["orphan_containers_stopped"] is True

    @pytest.mark.unit
    async def test_orphan_stop_failure_records_false_in_payload(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        origin_repo: Path,
    ) -> None:
        from unittest.mock import patch

        from awf.db.repositories.base import (
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )

        force_destroyed_between_guard_and_launch = False

        class _RecordingGit:
            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                del repo_url, base_branch
                return WorktreeLayout(
                    mirror_path=tmp_path / "mirror.git",
                    worktree_path=tmp_path / "worktrees" / workspace_id,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "e" * 40

        class _DelayedStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                nonlocal force_destroyed_between_guard_and_launch
                self.requests.append(request)
                assert force_destroyed_between_guard_and_launch, (
                    "force-destroy must happen before launch starts for this test"
                )
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_orphan_fail"),
                    compose_file=Path("/tmp/awf-compose/ws_orphan_fail/compose.yml"),
                )

        async def _force_destroy_after_guard(
            sf: async_sessionmaker[AsyncSession], workspace_id: str
        ) -> None:
            async with sf() as s:
                repo = WorkspaceRepository(s)
                ws = await repo.get(workspace_id)
                assert ws is not None
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.cancelled,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.destroying,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.destroyed,
                    reason_code="TEST_FORCE_DESTROY",
                )
                await repo.add_event(
                    ws,
                    event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    payload={
                        "compose_project_name": ws.compose_project_name,
                        "workspace_status": WorkspaceStatus.destroyed.value,
                        "cleanup": {
                            "stack_stopped": False,
                            "source": "force_destroy_workspace",
                        },
                    },
                )
                await s.commit()

        class _ForceDestroyProvisioner(Provisioner):
            async def _recheck_status(
                self,
                workspace_id: str,
                *,
                expected: WorkspaceStatus,
                action: str,
                reason_code: str,
            ) -> bool:
                return await super()._recheck_status(
                    workspace_id,
                    expected=expected,
                    action=action,
                    reason_code=reason_code,
                )

            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                result = await super()._recheck_before_launch(workspace_id)
                if result:
                    await _force_destroy_after_guard(session_factory, workspace_id)
                    nonlocal force_destroyed_between_guard_and_launch
                    force_destroyed_between_guard_and_launch = True
                return result

        async def _mock_stop_project_containers_fail(
            compose_project_name: str,
        ) -> None:
            raise RuntimeError("docker stop failed")

        launcher = _DelayedStackLauncher()
        provisioner = _ForceDestroyProvisioner(
            session_factory=session_factory,
            git=_RecordingGit(),
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
                resolved_profile=_secret_profile().model_dump(mode="json"),
            )
            await s.commit()
            ws_id = ws.id

        with patch(
            "awf.node.provisioner.stop_project_containers",
            new=_mock_stop_project_containers_fail,
        ):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            stale_skip_events = [
                e
                for e in reloaded.events
                if e.event_type == "workspace.stale_action_skipped"
                and e.reason_code == "TERMINAL_CLEANUP_WON_DURING_LAUNCH"
            ]
            assert len(stale_skip_events) == 1
            assert stale_skip_events[0].payload["orphan_containers_stopped"] is False
            assert "orphan_stop_error" in stale_skip_events[0].payload
            revoked_events = [
                e
                for e in reloaded.events
                if e.event_type == "workspace.terminal_runtime_release_revoked"
                and e.reason_code == "TERMINAL_RUNTIME_RELEASE_REVOKED_ORPHAN_STOP_FAILED"
            ]
            assert len(revoked_events) == 1, (
                "orphan stop failure must record a terminal_runtime_release_revoked event"
            )

    @pytest.mark.unit
    async def test_orphan_stop_timeout_records_false_in_payload(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import patch

        from awf.db.repositories.base import (
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            ws.status = WorkspaceStatus.destroyed.value
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await s.commit()
            ws_id = ws.id

        stop_started = asyncio.Event()
        never_finish = asyncio.Event()

        async def _mock_stop_project_containers_hangs(
            compose_project_name: str,
        ) -> None:
            assert compose_project_name == f"awf_{ws_id}"
            stop_started.set()
            await never_finish.wait()

        monkeypatch.setattr(
            "awf.node.provisioner._ORPHAN_STOP_TIMEOUT_SECONDS",
            0.01,
            raising=False,
        )
        with patch(
            "awf.node.provisioner.stop_project_containers",
            new=_mock_stop_project_containers_hangs,
        ):
            assert await asyncio.wait_for(
                provisioner._launch_lost_to_terminal_cleanup(ws_id),  # noqa: SLF001
                timeout=0.5,
            )

        assert stop_started.is_set()
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            stale_skip_events = [
                e
                for e in reloaded.events
                if e.event_type == "workspace.stale_action_skipped"
                and e.reason_code == "TERMINAL_CLEANUP_WON_DURING_LAUNCH"
            ]
            assert len(stale_skip_events) == 1
            assert stale_skip_events[0].payload["orphan_containers_stopped"] is False
            assert "timed out after 0.01s" in stale_skip_events[0].payload["orphan_stop_error"]
            revoked_events = [
                e
                for e in reloaded.events
                if e.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE
                and e.reason_code == TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE
            ]
            assert len(revoked_events) == 1, (
                "orphan stop timeout must record a terminal_runtime_release_revoked event"
            )

    @pytest.mark.unit
    async def test_orphan_stop_failure_over_cap_records_revoke_cap_escalation(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        from unittest.mock import patch

        from awf.db.repositories.base import (
            TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            ws.status = WorkspaceStatus.destroyed.value
            for _ in range(4):
                await repo.add_event(
                    ws,
                    event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                )
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            await s.commit()
            ws_id = ws.id

        async def _mock_stop_project_containers_fail(
            compose_project_name: str,
        ) -> None:
            del compose_project_name
            raise RuntimeError("docker stop failed")

        with patch(
            "awf.node.provisioner.stop_project_containers",
            new=_mock_stop_project_containers_fail,
        ):
            assert await provisioner._launch_lost_to_terminal_cleanup(ws_id) is True  # noqa: SLF001

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            revoked_events = [
                e
                for e in reloaded.events
                if e.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE
                and e.reason_code == TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE
            ]
            assert len(revoked_events) == 5, (
                "the current orphan stop failure must still record a revoke event"
            )
            cap_events = [
                e
                for e in reloaded.events
                if e.event_type == "workspace.stale_action_skipped"
                and e.reason_code == "REVOKE_CAP_REACHED"
            ]
            assert len(cap_events) == 1, (
                "over-cap revoke counts must still surface the operator escalation"
            )
            assert cap_events[0].payload["revoke_count"] == 5
            assert "lifetime-total revoke events" in cap_events[0].payload["message"]
            assert "consecutive revoke events" not in cap_events[0].payload["message"]
            assert "docker stop failed" in cap_events[0].payload["orphan_stop_error"]


class TestSecretLeaseIssueEdges:
    @pytest.mark.unit
    async def test_issue_secret_leases_skips_missing_or_stale_workspace(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        await provisioner._issue_secret_leases("ws_missing", _secret_profile())

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            ws.status = WorkspaceStatus.ready.value
            await s.commit()
            ws_id = ws.id

        await provisioner._issue_secret_leases(ws_id, _secret_profile())

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert leases == []
            skip_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.stale_action_skipped"
            ]
            assert skip_events[-1].payload == {
                "action": "issue_secret_leases",
                "expected_status": WorkspaceStatus.provisioning.value,
                "actual_status": WorkspaceStatus.ready.value,
            }

    @pytest.mark.unit
    async def test_issue_secret_leases_reraises_service_failure(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FailingSecretLeaseService:
            def __init__(self, session: AsyncSession) -> None:
                del session

            async def issue_profile_secret_leases(
                self,
                workspace: Any,
                profile: WorkspaceProfile,
            ) -> object:
                del workspace, profile
                raise RuntimeError("lease repository unavailable")

        monkeypatch.setattr(
            "awf.node.provisioner.SecretLeaseService",
            _FailingSecretLeaseService,
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
            ws.status = WorkspaceStatus.provisioning.value
            await s.commit()
            ws_id = ws.id

        with pytest.raises(RuntimeError, match="lease repository unavailable"):
            await provisioner._issue_secret_leases(ws_id, _secret_profile())
