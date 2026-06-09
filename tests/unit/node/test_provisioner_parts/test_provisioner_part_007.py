"""Provisioner pre-launch and terminal-cleanup edge coverage (split part 007).

These tests exercise defensive branches in :mod:`awf.node.provisioner` that the
happy-path and primary-failure suites in parts 001-006 do not reach:

* generic-exception fallbacks in the companion / auto-profile host-port checks
  (``COMPANION_HOST_PORT_CHECK_FATAL`` / ``AUTO_PROFILE_HOST_PORT_CHECK_FATAL``);
* the pre-launch ``compose_project_name`` persistence-commit failure
  (``PRE_LAUNCH_COMMIT_FATAL``);
* the stale-status recheck after companion materialization;
* the compose-fail backstop that re-persists ``compose_project_name`` /
  ``resolved_profile`` when they were never published;
* the duplicate companion host-port rejection and the workspace-vanished
  branches of ``_launch_lost_to_terminal_cleanup``.
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
from awf.db.repositories.base import (
    PRE_LAUNCH_FAILURE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
)
from awf.db.session import make_session_factory
from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.service.workspaces import WorkspaceCreateDuplicateHostPortError
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


class _RecordingStackLauncher:
    """Launcher that records requests and never actually starts a stack."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def launch(self, request: Any) -> object:
        self.requests.append(request)
        raise AssertionError("stack launch must not run for this scenario")


async def _signal_compose_up_started(request: Any) -> None:
    on_started = getattr(request, "on_compose_up_started", None)
    if on_started is not None:
        await on_started()


async def _create_requested_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
    *,
    task_title: str,
    requested_profile: dict[str, Any] | None = None,
    task_policy: dict[str, Any] | None = None,
) -> str:
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin_repo),
            branch_base="development",
            task_title=task_title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
            requested_profile=requested_profile,
            task_policy=task_policy,
        )
        await s.commit()
        return ws.id


class TestHostPortCheckGenericFailures:
    """Generic (non-typed) exceptions in the pre-launch host-port checks."""

    async def test_companion_host_port_check_generic_exception_marks_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async def _boom(**kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("companion port admission lock query exploded")

        monkeypatch.setattr(provisioner, "_check_companion_host_ports", _boom)

        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="companion-port-check-boom"
        )

        # Generic failure is swallowed into a terminal mark_failed (no re-raise).
        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message == (
                "companion host-port check failed; compose not started"
            )
            # Pre-launch failure must not leave a port-blocking compose project.
            assert reloaded.compose_project_name is None
            assert any(
                event.reason_code == "COMPANION_HOST_PORT_CHECK_FATAL" for event in reloaded.events
            )
            assert any(
                event.event_type == PRE_LAUNCH_FAILURE_EVENT_TYPE for event in reloaded.events
            )

    async def test_auto_profile_host_port_check_generic_exception_marks_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async def _boom(**kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("auto-profile port admission lock query exploded")

        monkeypatch.setattr(provisioner, "_check_auto_resolved_profile_host_ports", _boom)

        ws_id = await _create_requested_workspace(
            session_factory,
            origin_repo,
            task_title="auto-profile-port-check-boom",
            requested_profile={"name": "inline-noports"},
        )

        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message == (
                "auto-resolved profile host-port check failed; compose not started"
            )
            assert reloaded.compose_project_name is None
            assert any(
                event.reason_code == "AUTO_PROFILE_HOST_PORT_CHECK_FATAL"
                for event in reloaded.events
            )


class TestPreLaunchCommitFailure:
    """The pre-launch ``compose_project_name`` persistence commit failing."""

    async def test_pre_launch_commit_failure_marks_failed_without_compose_project(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        # The pre-launch block reads the row under SELECT FOR UPDATE before
        # committing compose_project_name. Failing that single read forces the
        # ``except`` path (PRE_LAUNCH_COMMIT_FATAL) without poisoning any other
        # session's commit.
        original_get_for_update = WorkspaceRepository.get_for_update
        triggered = {"count": 0}

        async def _failing_get_for_update(self: WorkspaceRepository, workspace_id: str) -> Any:
            # The first get_for_update in the pipeline is the pre-launch persist.
            if triggered["count"] == 0:
                triggered["count"] += 1
                raise RuntimeError("pre-launch row lock unavailable")
            return await original_get_for_update(self, workspace_id)

        monkeypatch.setattr(WorkspaceRepository, "get_for_update", _failing_get_for_update)

        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="pre-launch-commit-boom"
        )

        await provisioner.provision(ws_id)

        assert triggered["count"] == 1
        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message == (
                "pre-launch commit failed; compose_project_name not persisted"
            )
            assert reloaded.compose_project_name is None
            assert any(event.reason_code == "PRE_LAUNCH_COMMIT_FATAL" for event in reloaded.events)


class TestStaleStatusAfterMaterializeCompanions:
    """A cancel that lands during companion materialization aborts provisioning."""

    async def test_recheck_after_materialize_aborts_when_status_moved(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        launcher = _RecordingStackLauncher()

        class _CancelDuringMaterializeProvisioner(Provisioner):
            async def _materialize_companions(
                self,
                *,
                workspace_id: str,
                companions: tuple[WorkspaceCompanionSpec, ...],
                default_base_branch: str,
            ) -> tuple[MaterializedCompanionService, ...]:
                del companions, default_base_branch
                # Simulate a concurrent cancel committing a terminal transition
                # while companion worktrees are being materialized.
                async with self._session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get_for_update(workspace_id)
                    assert ws is not None
                    await repo.transition(
                        ws, to=WorkspaceStatus.cancelled, reason_code="TEST_CANCEL"
                    )
                    await s.commit()
                return ()

        provisioner = _CancelDuringMaterializeProvisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="cancel-during-materialize"
        )

        # Returns cleanly (no raise): the recheck observes the cancel and aborts.
        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            # The cancel won; provisioning must not have overwritten it.
            assert reloaded.status == WorkspaceStatus.cancelled.value
            assert reloaded.failure_reason is None


class TestComposeFailBackstopRepublishesMetadata:
    """The compose-fail backstop re-persists metadata that was never published."""

    async def test_backstop_sets_compose_project_and_resolved_profile(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _FailingComposeLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="docker compose up failed after launch began",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _ClearComposeProjectProvisioner(Provisioner):
            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                # Pre-launch already persisted compose_project_name and the
                # auto-resolved profile; reset both to NULL so the compose-fail
                # backstop's "compose_project_name is None" branch (normally
                # dead) runs and re-persists them.
                async with self._session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get_for_update(workspace_id)
                    assert ws is not None
                    assert ws.compose_project_name == f"awf_{workspace_id}"
                    assert ws.resolved_profile is not None
                    ws.compose_project_name = None
                    ws.resolved_profile = None
                    await s.commit()
                return True

        provisioner = _ClearComposeProjectProvisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        # An inline (auto-resolved) profile with no host ports: resolution
        # produces resolved_profile_dict, but it is never published to the row
        # because the host-port check returns early. The backstop must therefore
        # set both compose_project_name and resolved_profile.
        ws_id = await _create_requested_workspace(
            session_factory,
            origin_repo,
            task_title="compose-fail-backstop-republish",
            requested_profile={"name": "inline-backstop"},
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.resolved_profile is None

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            # Backstop re-persisted both pieces of cleanup metadata.
            assert reloaded.compose_project_name == f"awf_{ws_id}"
            assert reloaded.resolved_profile is not None
            assert reloaded.resolved_profile["name"] == "inline-backstop"


class TestComposeFailBackstopWithStoredProfile:
    """Backstop re-persists compose project but skips an already-stored profile."""

    async def test_backstop_sets_compose_project_without_touching_stored_profile(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _FailingComposeLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="docker compose up failed after launch began",
                    reason_code="COMPOSE_UP_FAILED",
                )

        class _ClearComposeProjectProvisioner(Provisioner):
            async def _recheck_before_launch(self, workspace_id: str) -> bool:
                async with self._session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get_for_update(workspace_id)
                    assert ws is not None
                    assert ws.compose_project_name == f"awf_{workspace_id}"
                    ws.compose_project_name = None
                    await s.commit()
                return True

        provisioner = _ClearComposeProjectProvisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        # A *stored* resolved_profile means no provision-time auto-resolution, so
        # resolved_profile_dict is None: the backstop must re-persist
        # compose_project_name but must not touch the already-stored profile.
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="compose-fail-backstop-stored-profile",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                resolved_profile={"name": "stored-profile"},
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.compose_project_name == f"awf_{ws_id}"
            # The stored profile is untouched by the backstop.
            assert reloaded.resolved_profile is not None
            assert reloaded.resolved_profile["name"] == "stored-profile"


class TestComposeFailBackstopCommitVerifyFailure:
    """The verify-after-mark_failed read failing inside the backstop handler."""

    async def test_verify_session_failure_reraises_original_compose_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_failed = {"flag": False}
        original_commit = AsyncSession.commit

        class _FailingComposeLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                compose_failed["flag"] = True
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="docker compose up failed after launch began",
                    reason_code="COMPOSE_UP_FAILED",
                )

        commit_calls = {"count": 0}

        async def _fail_first_post_compose_commit(self: AsyncSession) -> None:
            # Fail only the very first commit after compose blew up: that is the
            # backstop's compose_project_name persistence commit, which routes
            # into the COMPOSE_FAIL_COMMIT_FATAL handler.
            if compose_failed["flag"] and commit_calls["count"] == 0:
                commit_calls["count"] += 1
                raise RuntimeError("compose-fail backstop commit unavailable")
            await original_commit(self)

        # The verify read at the end of the backstop handler must fail, but
        # _mark_failed (which runs just before it) also reads the row via
        # WorkspaceRepository.get internally. Gate the verify failure on the
        # FATAL mark_failed having completed so only the verify session's read
        # raises.
        fatal_mark_failed_done = {"flag": False}
        original_mark_failed = Provisioner._mark_failed

        async def _tracked_mark_failed(self: Provisioner, **kwargs: Any) -> None:
            await original_mark_failed(self, **kwargs)
            if kwargs.get("reason_code") == "COMPOSE_FAIL_COMMIT_FATAL":
                fatal_mark_failed_done["flag"] = True

        original_get = WorkspaceRepository.get
        verify_attempts = {"count": 0}

        async def _failing_verify_get(self: WorkspaceRepository, workspace_id: str) -> Any:
            # Only the verify read (after the FATAL mark_failed) raises, so the
            # inner verify-guard ``except`` branch runs and must not mask the
            # original compose error.
            if fatal_mark_failed_done["flag"] and verify_attempts["count"] == 0:
                verify_attempts["count"] += 1
                raise RuntimeError("verify read lost its DB session")
            return await original_get(self, workspace_id)

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="compose-fail-verify-boom"
        )

        monkeypatch.setattr(AsyncSession, "commit", _fail_first_post_compose_commit)
        monkeypatch.setattr(Provisioner, "_mark_failed", _tracked_mark_failed)
        monkeypatch.setattr(WorkspaceRepository, "get", _failing_verify_get)

        # The verify read failed, so the handler cannot confirm the row reached
        # ``failed`` and re-raises the original ComposeOperationError chained to
        # the commit error.
        with pytest.raises(ComposeOperationError) as raised:
            await provisioner.provision(ws_id)

        assert raised.value.reason_code == "COMPOSE_UP_FAILED"
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert commit_calls["count"] == 1
        assert verify_attempts["count"] == 1

    async def test_verify_finds_non_failed_row_reraises_compose_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_failed = {"flag": False}
        original_commit = AsyncSession.commit

        class _FailingComposeLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                compose_failed["flag"] = True
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr="docker compose up failed after launch began",
                    reason_code="COMPOSE_UP_FAILED",
                )

        commit_calls = {"count": 0}

        async def _fail_first_post_compose_commit(self: AsyncSession) -> None:
            if compose_failed["flag"] and commit_calls["count"] == 0:
                commit_calls["count"] += 1
                raise RuntimeError("compose-fail backstop commit unavailable")
            await original_commit(self)

        # Stub the FATAL mark_failed to a no-op so the row stays ``provisioning``.
        # The verify read then finds a non-failed row, so the guard cannot return
        # early and re-raises the original ComposeOperationError instead.
        fatal_calls = {"count": 0}
        original_mark_failed = Provisioner._mark_failed

        async def _noop_fatal_mark_failed(self: Provisioner, **kwargs: Any) -> None:
            if kwargs.get("reason_code") == "COMPOSE_FAIL_COMMIT_FATAL":
                fatal_calls["count"] += 1
                return
            await original_mark_failed(self, **kwargs)

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="compose-fail-verify-nonfailed"
        )

        monkeypatch.setattr(AsyncSession, "commit", _fail_first_post_compose_commit)
        monkeypatch.setattr(Provisioner, "_mark_failed", _noop_fatal_mark_failed)

        with pytest.raises(ComposeOperationError) as raised:
            await provisioner.provision(ws_id)

        assert raised.value.reason_code == "COMPOSE_UP_FAILED"
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert fatal_calls["count"] == 1
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            # mark_failed was a no-op, so the row never reached ``failed``.
            assert reloaded.status == WorkspaceStatus.provisioning.value


class TestCheckCompanionHostPortsDuplicate:
    """Two companions claiming the same host port are rejected before any DB I/O."""

    async def test_duplicate_companion_host_port_raises_without_session(self) -> None:
        session_factory = AsyncMock()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=AsyncMock(),
            stack_launcher=None,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        task_policy = {
            "companions": [
                {
                    "name": "web",
                    "repo_url": "git@github.com:example/web.git",
                    "ports": [[80, 18080]],
                },
                {
                    "name": "api",
                    "repo_url": "git@github.com:example/api.git",
                    "ports": [[80, 18080]],
                },
            ]
        }

        with pytest.raises(WorkspaceCreateDuplicateHostPortError) as exc_info:
            await provisioner._check_companion_host_ports(
                task_policy=task_policy,
                excluding_workspace_id="ws-1",
            )

        assert exc_info.value.host_port == 18080
        # Duplicate is caught in-memory before any session is opened.
        assert session_factory.call_count == 0


class TestLaunchLostToTerminalCleanupWorkspaceVanished:
    """Both vanished-workspace branches of the launch-lost cleanup guard."""

    async def test_returns_false_when_workspace_absent_on_first_read(
        self, provisioner: Provisioner
    ) -> None:
        # No row exists: the guard cannot observe a terminal release, so the
        # caller is told it is still clear to proceed.
        result = await provisioner._launch_lost_to_terminal_cleanup("ws-does-not-exist")
        assert result is False

    async def test_returns_true_when_workspace_deleted_after_docker_io(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="cleanup-row-deleted"
        )
        # Seed a terminal_runtime_released event so the guard reaches the Docker
        # stop + reacquire path.
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(ws_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST_CLAIMED")
            await repo.add_event(
                ws,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                payload={"workspace_id": ws_id},
            )
            await s.commit()

        async def _delete_workspace_during_stop(_project: str) -> None:
            # Hard-delete the row while "Docker I/O" runs, between the guard's
            # two DB reads, so the post-stop get_for_update returns None.
            async with session_factory() as s:
                deleted = await WorkspaceRepository(s).get(ws_id)
                assert deleted is not None
                await s.delete(deleted)
                await s.commit()

        monkeypatch.setattr(
            "awf.node.provisioner.stop_project_containers",
            _delete_workspace_during_stop,
        )

        result = await provisioner._launch_lost_to_terminal_cleanup(ws_id)
        # Row vanished after the orphan stop; the guard still reports cleanup won.
        assert result is True

        async with session_factory() as s:
            assert await WorkspaceRepository(s).get(ws_id) is None


class TestComposeFailureBeforeLaunchSignaled:
    """ComposeOperationError raised before on_compose_up_started fires."""

    async def test_pre_signal_compose_failure_clears_compose_project_and_reraises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _PreSignalFailingLauncher:
            """Raises ComposeOperationError without ever signaling launch start."""

            async def launch(self, request: Any) -> object:
                del request  # never call on_compose_up_started
                raise ComposeOperationError(
                    operation="up",
                    returncode=125,
                    stdout="",
                    stderr="compose render failed before any container started",
                    reason_code="COMPOSE_RENDER_FAILED",
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_PreSignalFailingLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="compose-fail-before-signal"
        )

        # stack_launch_started is False, so _mark_failed clears the pre-published
        # compose_project_name and the original error is re-raised.
        with pytest.raises(ComposeOperationError) as raised:
            await provisioner.provision(ws_id)

        assert raised.value.reason_code == "COMPOSE_RENDER_FAILED"
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            reloaded = await repo.get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "infrastructure_failure"
            assert reloaded.failure_message is not None
            assert "compose render failed" in reloaded.failure_message
            # Compose never started, so the project must not block host ports.
            assert reloaded.compose_project_name is None


class TestUnexpectedFailureTerminalCleanupWon:
    """A generic launch failure where terminal cleanup already won returns early."""

    async def test_unexpected_failure_returns_early_when_cleanup_won(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _UnexpectedAfterSignalLauncher:
            async def launch(self, request: Any) -> object:
                await _signal_compose_up_started(request)
                raise RuntimeError("docker daemon connection dropped mid-launch")

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_UnexpectedAfterSignalLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="unexpected-cleanup-won"
        )

        mark_failed_called = {"flag": False}
        original_mark_failed = provisioner._mark_failed

        async def _tracking_mark_failed(**kwargs: Any) -> None:
            mark_failed_called["flag"] = True
            await original_mark_failed(**kwargs)

        # The generic handler must consult the terminal-cleanup guard first; when
        # it reports cleanup already won, provisioning returns without marking
        # failed (the workspace is already terminal).
        monkeypatch.setattr(provisioner, "_mark_failed", _tracking_mark_failed)
        monkeypatch.setattr(
            provisioner,
            "_launch_lost_to_terminal_cleanup_best_effort",
            AsyncMock(return_value=True),
        )

        # Returns cleanly: no re-raise because the workspace already reached a
        # terminal state via the winning cleanup.
        await provisioner.provision(ws_id)

        assert mark_failed_called["flag"] is False


class TestMarkFailedPreservesExistingNodeId:
    """_mark_failed must not overwrite a node_id that is already assigned."""

    async def test_existing_node_id_is_not_overwritten(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )

        async def _boom(**kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("companion port admission lock query exploded")

        monkeypatch.setattr(provisioner, "_check_companion_host_ports", _boom)

        ws_id = await _create_requested_workspace(
            session_factory, origin_repo, task_title="preexisting-node-id"
        )
        # Pre-assign a node_id (e.g. a prior provisioning attempt on another
        # node) so the ``ws.node_id is None`` guard in _mark_failed is skipped.
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get_for_update(ws_id)
            assert ws is not None
            ws.node_id = "other-node-99"
            await s.commit()

        await provisioner.provision(ws_id)

        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            # The pre-existing attribution must survive the failure path.
            assert reloaded.node_id == "other-node-99"


class TestRecheckBeforeLaunchWorkspaceAbsent:
    """_recheck_before_launch returns False when the row has vanished."""

    async def test_returns_false_when_workspace_absent(self, provisioner: Provisioner) -> None:
        result = await provisioner._recheck_before_launch("ws-does-not-exist")
        assert result is False
