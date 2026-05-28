"""Provisioner tests — real GitManager against a throwaway git repo + PostgreSQL DB.

We exercise the full provisioner flow rather than mocking git, because the whole
point is the integration between state transitions and filesystem operations.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import EgressDecision, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    EgressAuditRepository,
    ResourceReservationRepository,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeProjectPaths
from awf.node.git_manager import GitManager, WorktreeLayout
from awf.node.provisioner import (
    Provisioner,
    ProvisionerConfig,
    _egress_plan_decision,
    _egress_plan_destination_category,
    _positive_int,
    _provision_checkout_base_branch,
    _provision_local_branch_name,
    _provision_remote_push_branch,
)
from awf.profiles.models import EgressMode, ProfileSecret, WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "expected_category"),
    [
        (EgressMode.open, "public_internet"),
        (EgressMode.offline, "internal_only"),
        (EgressMode.restricted, "policy_decision"),
    ],
)
def test_egress_plan_destination_category_keeps_restricted_distinct_from_offline(
    mode: EgressMode,
    expected_category: str,
) -> None:
    assert _egress_plan_destination_category(mode) == expected_category


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "expected_decision"),
    [
        (EgressMode.open, EgressDecision.allow),
        (EgressMode.offline, EgressDecision.deny),
        (EgressMode.restricted, EgressDecision.deferred),
    ],
)
def test_egress_plan_decision_maps_profile_modes(
    mode: EgressMode,
    expected_decision: EgressDecision,
) -> None:
    assert _egress_plan_decision(mode) == expected_decision


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (7, 7),
        (0, None),
        (" 5 ", 5),
        ("0", None),
        ("five", None),
    ],
)
def test_positive_int_accepts_only_positive_int_values(value: object, expected: int | None) -> None:
    assert _positive_int(value) == expected


class TestSuccess:
    @pytest.mark.unit
    async def test_provision_claimed_missing_workspace_is_noop(
        self,
        provisioner: Provisioner,
    ) -> None:
        await provisioner.provision_claimed("ws_missing")

    @pytest.mark.unit
    async def test_provision_claimed_ready_workspace_records_stale_skip(
        self,
        provisioner: Provisioner,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="stale",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
            await s.commit()
            ws_id = ws.id

        await provisioner.provision_claimed(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.events[-1].event_type == "workspace.stale_action_skipped"
            assert reloaded.events[-1].payload["action"] == "provision"

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
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_launcher"),
                    compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
                )

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
            assert reloaded.compose_file_path == "/tmp/awf-compose/ws_launcher/compose.yml"

    @pytest.mark.unit
    async def test_materializes_companion_worktrees_before_stack_launch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingGit:
            work_dir = tmp_path / "awf-work"

            def __init__(self) -> None:
                self.add_worktree_calls: list[dict[str, object]] = []

            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                self.add_worktree_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "repo_url": repo_url,
                        "base_branch": base_branch,
                        "new_branch": new_branch,
                    }
                )
                worktree = self.work_dir / "worktrees" / workspace_id
                worktree.mkdir(parents=True, exist_ok=True)
                return WorktreeLayout(
                    mirror_path=self.work_dir / "mirrors" / f"{workspace_id}.git",
                    worktree_path=worktree,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "a" * 40

        class _RecordingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_companion"),
                    compose_file=Path("/tmp/awf-compose/ws_companion/compose.yml"),
                )

        git = _RecordingGit()
        launcher = _RecordingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git,  # type: ignore[arg-type]
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/app.git",
                branch_base="development",
                task_title="companions",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                task_policy={
                    "companions": [
                        {
                            "name": "backend",
                            "repo_url": "git@github.com:example/backend.git",
                            "base_branch": "main",
                            "build_context": "services/api",
                        },
                        {
                            "name": "worker",
                            "repo_url": "git@github.com:example/worker.git",
                        },
                    ]
                },
            )
            await s.commit()
            workspace_id = ws.id

        await provisioner.provision(workspace_id)

        assert git.add_worktree_calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "development",
                "new_branch": f"awf/{workspace_id}",
            },
            {
                "workspace_id": f"{workspace_id}__companion__backend",
                "repo_url": "git@github.com:example/backend.git",
                "base_branch": "main",
                "new_branch": f"awf/{workspace_id}/companion/backend",
            },
            {
                "workspace_id": f"{workspace_id}__companion__worker",
                "repo_url": "git@github.com:example/worker.git",
                "base_branch": "development",
                "new_branch": f"awf/{workspace_id}/companion/worker",
            },
        ]
        companion = launcher.requests[0].companions[0]
        assert companion.spec.name == "backend"
        assert companion.spec.build_context == "services/api"
        assert companion.layout.worktree_path == (
            tmp_path / "awf-work" / "worktrees" / f"{workspace_id}__companion__backend"
        )
        defaulted_companion = launcher.requests[0].companions[1]
        assert defaulted_companion.spec.name == "worker"
        assert defaulted_companion.spec.base_branch is None
        assert launcher.requests[0].companion_graph_prevalidated is True

    @pytest.mark.unit
    async def test_rejects_invalid_companion_graph_before_materializing_companions(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from awf.node.companion_services import validate_companion_service_graph
        from awf.profiles.compose import profile_services

        class _RecordingGit:
            work_dir = tmp_path / "awf-work"

            def __init__(self) -> None:
                self.add_worktree_calls: list[dict[str, object]] = []

            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                self.add_worktree_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "repo_url": repo_url,
                        "base_branch": base_branch,
                        "new_branch": new_branch,
                    }
                )
                worktree = self.work_dir / "worktrees" / workspace_id
                worktree.mkdir(parents=True, exist_ok=True)
                return WorktreeLayout(
                    mirror_path=self.work_dir / "mirrors" / f"{workspace_id}.git",
                    worktree_path=worktree,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "b" * 40

        class _ValidatingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                services = profile_services(
                    request.profile,
                    base_path=request.layout.worktree_path,
                )
                validate_companion_service_graph(
                    profile_services=services,
                    companions=request.companions,
                    docker_mode=request.profile.docker.mode,
                )
                raise AssertionError("invalid companion graph should fail before launch")

        git = _RecordingGit()
        launcher = _ValidatingStackLauncher()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git,  # type: ignore[arg-type]
            stack_launcher=launcher,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url="git@github.com:example/app.git",
                branch_base="development",
                task_title="companions",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                requested_profile={
                    "name": "colliding-companion",
                    "services": [{"name": "backend", "image": "redis:7-alpine"}],
                },
                task_policy={
                    "companions": [
                        {
                            "name": "backend",
                            "repo_url": "git@github.com:example/backend.git",
                        }
                    ]
                },
            )
            await s.commit()
            workspace_id = ws.id

        with pytest.raises(ProfileResolutionError) as raised:
            await provisioner.provision(workspace_id)

        assert raised.value.reason_code == "COMPANION_SERVICE_NAME_COLLISION"
        assert git.add_worktree_calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": "git@github.com:example/app.git",
                "base_branch": "development",
                "new_branch": f"awf/{workspace_id}",
            },
        ]
        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(workspace_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "profile_resolution_failure"

    @pytest.mark.unit
    async def test_rejects_profile_only_invalid_service_graph_before_secret_leases(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        from awf.node.companion_services import validate_companion_service_graph
        from awf.profiles.compose import profile_services

        class _ValidatingStackLauncher:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            async def launch(self, request: Any) -> object:
                self.requests.append(request)
                services = profile_services(
                    request.profile,
                    base_path=request.layout.worktree_path,
                )
                validate_companion_service_graph(
                    profile_services=services,
                    companions=request.companions,
                    docker_mode=request.profile.docker.mode,
                )
                raise AssertionError("invalid profile service graph should fail before launch")

        launcher = _ValidatingStackLauncher()
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
                task_title="profile services",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                resolved_profile={
                    "name": "profile-only-invalid-dependency",
                    "services": [
                        {"name": "cache", "image": "redis:7-alpine"},
                        {
                            "name": "api",
                            "image": "python:3.12-alpine",
                            "depends_on": ["cache"],
                        },
                    ],
                    "secrets": [
                        {
                            "name": "api-token",
                            "kind": "env",
                            "target": "API_TOKEN",
                            "provider": "env",
                            "ref": "env/API_TOKEN",
                        }
                    ],
                },
            )
            await s.commit()
            workspace_id = ws.id

        with pytest.raises(ProfileResolutionError) as raised:
            await provisioner.provision(workspace_id)

        assert raised.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_UNHEALTHY"
        assert launcher.requests == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(workspace_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "profile_resolution_failure"
            leases = await SecretLeaseRepository(s).list_for_workspace(workspace_id)
            assert leases == []

    @pytest.mark.unit
    async def test_sync_feature_pr_checks_out_pull_head_ref_and_records_remote_push_branch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        class _RecordingGit:
            work_dir = tmp_path / "awf-work"

            def __init__(self) -> None:
                self.add_worktree_calls: list[dict[str, object]] = []

            async def add_worktree(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                base_branch: str,
                new_branch: str,
            ) -> WorktreeLayout:
                self.add_worktree_calls.append(
                    {
                        "workspace_id": workspace_id,
                        "repo_url": repo_url,
                        "base_branch": base_branch,
                        "new_branch": new_branch,
                    }
                )
                worktree = self.work_dir / "worktrees" / workspace_id
                worktree.mkdir(parents=True, exist_ok=True)
                return WorktreeLayout(
                    mirror_path=self.work_dir / "mirrors" / "repo.git",
                    worktree_path=worktree,
                    branch_name=new_branch,
                )

            async def head_sha(self, *, workspace_id: str) -> str:
                del workspace_id
                return "h" * 40

        git = _RecordingGit()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git,  # type: ignore[arg-type]
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url="https://github.com/dimileeh/aira-web.git",
                branch_base="development",
                task_title="adopt",
                task_prompt="monitor",
                agent="codex",
                test_commands=[],
                task_kind="sync_feature_pr",
                task_policy={
                    "pr_adoption": {
                        "pr_number": 277,
                        "head_ref": "feature/ready",
                        "base_ref": "development",
                    }
                },
                resolved_profile={"name": "generic"},
            )
            ws.pr_number = 277
            await s.commit()
            workspace_id = ws.id

        await provisioner.provision(workspace_id)

        assert git.add_worktree_calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": "https://github.com/dimileeh/aira-web.git",
                "base_branch": "refs/pull/277/head",
                "new_branch": f"feature-sync/{workspace_id}",
            }
        ]
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(workspace_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.ready.value
            assert reloaded.branch_base == "development"
            assert reloaded.branch_name == f"feature-sync/{workspace_id}"
            assert reloaded.remote_push_branch == "feature/ready"

    @pytest.mark.unit
    def test_sync_feature_pr_checkout_uses_pull_head_ref_when_pr_number_is_present(
        self,
    ) -> None:
        ws = Workspace(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_base="development",
            branch_name="feature-sync/ws",
            remote_push_branch="feature/fork-head",
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "pr_number": "278",
                    "head_ref": "feature/fork-head",
                    "base_ref": "development",
                }
            },
        )

        assert _provision_checkout_base_branch(ws) == "refs/pull/278/head"
        assert _provision_remote_push_branch(ws) == "feature/fork-head"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_kind, task_policy",
        [
            ("feature_branch_pr", {"pr_adoption": {"head_ref": "feature/ignored"}}),
            ("sync_feature_pr", {}),
            ("sync_feature_pr", {"pr_adoption": "not-a-dict"}),
            ("sync_feature_pr", {"pr_adoption": {"head_ref": 123}}),
            ("sync_feature_pr", {"pr_adoption": {"head_ref": " "}}),
        ],
    )
    def test_sync_feature_pr_head_ref_helpers_fall_back_when_metadata_is_absent(
        self,
        task_kind: str,
        task_policy: dict[str, Any],
    ) -> None:
        ws = Workspace(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_base="development",
            branch_name="awf/ws",
            remote_push_branch="awf/ws",
            task_kind=task_kind,
            task_policy=task_policy,
        )

        assert _provision_checkout_base_branch(ws) == "development"
        assert _provision_remote_push_branch(ws) == "awf/ws"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "task_policy, expected_source",
        [
            (
                {"release_sync": {"source_branch": "release/next", "target_branch": "main"}},
                "release/next",
            ),
            ({"release_sync": {"target_branch": "main"}}, "development"),
            ({}, "development"),
            ({"release_sync": "not-a-dict"}, "development"),
            ({"release_sync": {"source_branch": " "}}, "development"),
        ],
    )
    def test_sync_release_pr_checks_out_source_branch(
        self,
        task_policy: dict[str, Any],
        expected_source: str,
    ) -> None:
        ws = Workspace(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_base="main",
            branch_name=None,
            remote_push_branch=None,
            task_kind="sync_release_pr",
            task_policy=task_policy,
        )

        assert _provision_local_branch_name(ws, workspace_id="ws1", branch_prefix="awf") == (
            "release-sync/ws1"
        )
        assert _provision_checkout_base_branch(ws) == expected_source
        assert _provision_remote_push_branch(ws) == expected_source

    @pytest.mark.unit
    def test_feature_branch_pr_local_branch_uses_prefix(self) -> None:
        ws = Workspace(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_base="main",
            task_kind="feature_branch_pr",
            task_policy={},
        )
        assert (
            _provision_local_branch_name(ws, workspace_id="ws1", branch_prefix="awf") == "awf/ws1"
        )

    @pytest.mark.unit
    async def test_profile_secret_leases_are_issued_before_launch_and_mounted_after_success(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _RecordingStackLauncher:
            async def launch(self, request: Any) -> object:
                async with session_factory() as s:
                    workspace = await WorkspaceRepository(s).get(request.workspace_id)
                    assert workspace is not None
                    events = [
                        event.reason_code
                        for event in workspace.events
                        if event.event_type == "workspace.secret_lease"
                    ]
                    assert events == ["SECRET_LEASE_ISSUED"]
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_secret"),
                    compose_file=Path("/tmp/awf-compose/ws_secret/compose.yml"),
                )

        profile = {
            "name": "provisioner-secrets",
            "secrets": [
                {
                    "name": "api-token",
                    "kind": "env",
                    "target": "API_TOKEN",
                    "provider": "env",
                    "ref": "env/API_TOKEN",
                }
            ],
        }
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_RecordingStackLauncher(),
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
                resolved_profile=profile,
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert len(leases) == 1
            assert leases[0].status == "mounted"
            assert leases[0].mounted_at is not None
            reason_codes = [
                event.reason_code
                for event in reloaded.events
                if event.event_type == "workspace.secret_lease"
            ]
            assert reason_codes == ["SECRET_LEASE_ISSUED", "SECRET_LEASE_MOUNTED"]

    @pytest.mark.unit
    async def test_profile_secret_lease_mount_metadata_uses_sanitized_stack_plan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        raw_secret = "sk-live-do-not-store-in-metadata"
        raw_ref = "env/OPENAI_API_KEY"

        class _RecordingStackLauncher:
            async def launch(self, request: Any) -> object:
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_secret"),
                    compose_file=Path("/tmp/awf-compose/ws_secret/compose.yml"),
                    secret_lease_mount_metadata={
                        "schema": "secret_lease_mount_metadata.v1",
                        "mount_plan": "profile_declared_secret_leases",
                        "env_count": 2,
                        "mount_count": 1,
                        "providers": ["env", "github", "local-auth"],
                        "targets": [
                            "OPENAI_API_KEY",
                            "GH_TOKEN",
                            "/home/agent/.config/gh",
                        ],
                        "secret_value": raw_secret,
                        "ref": raw_ref,
                    },
                )

        profile = {
            "name": "provisioner-secrets",
            "secrets": [
                {
                    "name": "openai",
                    "kind": "env",
                    "target": "OPENAI_API_KEY",
                    "provider": "env",
                    "ref": raw_ref,
                },
                {
                    "name": "github",
                    "kind": "env",
                    "target": "GH_TOKEN",
                    "provider": "github",
                    "ref": "token",
                },
                {
                    "name": "github-cli-config",
                    "kind": "mount",
                    "target": "/home/agent/.config/gh",
                    "provider": "local-auth",
                    "ref": ".config/gh",
                },
            ],
        }
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_RecordingStackLauncher(),
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
                resolved_profile=profile,
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert len(leases) == 3
            metadata = leases[0].mount_metadata
            assert metadata == {
                "schema": "secret_lease_mount_metadata.v1",
                "mount_plan": "profile_declared_secret_leases",
                "env_count": 2,
                "mount_count": 1,
                "providers": ["env", "github", "local-auth"],
                "targets": [
                    "OPENAI_API_KEY",
                    "GH_TOKEN",
                    "/home/agent/.config/gh",
                ],
                "compose_project": f"awf_{ws_id}",
                "compose_file": "/tmp/awf-compose/ws_secret/compose.yml",
            }
            rendered = json.dumps([lease.mount_metadata for lease in leases], default=str)
            assert raw_secret not in rendered
            assert raw_ref not in rendered

    @pytest.mark.unit
    async def test_companion_secret_metadata_event_persists_without_profile_secret_leases(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        raw_secret = "sk-live-do-not-store-in-event"

        class _RecordingStackLauncher:
            async def launch(self, request: Any) -> object:
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_companion"),
                    compose_file=Path("/tmp/awf-compose/ws_companion/compose.yml"),
                    secret_lease_mount_metadata={
                        "schema": "secret_lease_mount_metadata.v1",
                        "companion_env_secret_count": 1,
                        "companion_env_secrets": (
                            {
                                "companion": "reviewer",
                                "target": "ANTHROPIC_API_KEY",
                                "provider": "env",
                                "source": "ANTHROPIC_API_KEY",
                                "required": True,
                            },
                        ),
                        "companion_omitted_optional_env_secret_count": 1,
                        "companion_omitted_optional_env_secrets": (
                            {
                                "companion": "reviewer",
                                "target": "OPENAI_API_KEY",
                                "provider": "env",
                                "source": "OPENAI_API_KEY",
                                "required": False,
                            },
                        ),
                        "secret_value": raw_secret,
                    },
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_RecordingStackLauncher(),
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
                resolved_profile={"name": "companion-only-secrets"},
            )
            await s.commit()
            ws_id = ws.id

        await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert await SecretLeaseRepository(s).list_for_workspace(ws_id) == []
            events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.companion_env_secret_metadata"
            ]
            assert len(events) == 1
            event = events[0]
            assert event.reason_code == "COMPANION_ENV_SECRET_METADATA_RECORDED"
            assert event.payload == {
                "schema": "companion_env_secret_stack_metadata.v1",
                "compose_project": f"awf_{ws_id}",
                "compose_file": "/tmp/awf-compose/ws_companion/compose.yml",
                "companion_env_secret_count": 1,
                "companion_env_secrets": [
                    {
                        "companion": "reviewer",
                        "target": "ANTHROPIC_API_KEY",
                        "provider": "env",
                        "source": "ANTHROPIC_API_KEY",
                        "required": True,
                    }
                ],
                "companion_omitted_optional_env_secret_count": 1,
                "companion_omitted_optional_env_secrets": [
                    {
                        "companion": "reviewer",
                        "target": "OPENAI_API_KEY",
                        "provider": "env",
                        "source": "OPENAI_API_KEY",
                        "required": False,
                    }
                ],
            }
            assert raw_secret not in json.dumps(event.payload, default=str)

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
