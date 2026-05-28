"""Provisioner failure-path coverage split from the main provisioner part."""

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
    SecretLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError, ComposeProjectPaths
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.stack_launcher import ComposeStackLauncher
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


class TestFailureHandling:
    @pytest.mark.unit
    async def test_invalid_inline_profile_marks_workspace_failed_as_profile_resolution(
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
                requested_profile={"name": ""},
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ProfileResolutionError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "profile_resolution_failure"
            assert reloaded.failure_message is not None
            assert "name" in reloaded.failure_message

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
            # Stack launch may have created Docker resources on this node before
            # raising. ``_mark_failed`` must attribute the failed row to this
            # node so the terminal runtime release sweep targets only the
            # Docker daemon that could hold those resources.
            assert reloaded.node_id == "test-node-01"

    @pytest.mark.unit
    async def test_stack_startup_failure_records_computed_egress_audit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _FailingStackLauncher:
            async def launch(self, request: Any) -> object:
                del request
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
                resolved_profile={
                    "name": "restricted-audit",
                    "security": {"egress": {"mode": "restricted"}},
                },
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            audit = await EgressAuditRepository(s).get_latest_for_workspace(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert audit is not None
            assert audit.policy_posture == "restricted"
            assert audit.decision == "deferred"
            assert audit.destination_category == "policy_decision"
            assert audit.reason_code == "LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY"
            assert audit.details["destination_filtering"] == "deferred"

    @pytest.mark.unit
    async def test_stack_startup_failure_marks_failed_when_egress_audit_write_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _FailingStackLauncher:
            async def launch(self, request: Any) -> object:
                del request
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

        async def _raise_audit_write(**kwargs: Any) -> bool:
            del kwargs
            raise RuntimeError("egress_audit_records missing")

        monkeypatch.setattr(
            provisioner,
            "_record_egress_audit_if_current",
            _raise_audit_write,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=str(origin_repo),
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
                resolved_profile={
                    "name": "restricted-audit",
                    "security": {"egress": {"mode": "restricted"}},
                },
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            audit = await EgressAuditRepository(s).get_latest_for_workspace(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            assert reloaded.failure_message is not None
            assert "docker compose up failed" in reloaded.failure_message
            assert audit is None

    @pytest.mark.unit
    async def test_stack_launch_failure_revokes_issued_secret_leases_without_hiding_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        raw_ref = "provider/path/not-a-token-value"

        class _FailingStackLauncher:
            async def launch(self, request: Any) -> object:
                raise ComposeOperationError(
                    operation="up",
                    returncode=17,
                    stdout="",
                    stderr=f"compose up failed for {raw_ref}",
                    reason_code="COMPOSE_UP_FAILED",
                )

        profile = {
            "name": "failing-secrets",
            "secrets": [
                {
                    "name": "api-token",
                    "kind": "env",
                    "target": "API_TOKEN",
                    "provider": "vault",
                    "ref": raw_ref,
                }
            ],
        }
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
                resolved_profile=profile,
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ComposeOperationError, match="compose up failed"):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert len(leases) == 1
            assert leases[0].status == "revoked"
            assert leases[0].revoke_reason_code == "PROVISIONING_FAILED"
            payloads = [
                event.payload
                for event in reloaded.events
                if event.event_type == "workspace.secret_lease"
            ]
            assert raw_ref not in str(payloads)
            assert [event.reason_code for event in reloaded.events].count(
                "SECRET_LEASE_REVOKED"
            ) == 1

    @pytest.mark.unit
    async def test_legacy_allowlist_profile_marks_resolution_failed_before_compose_up(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _RecordingCompose:
            def __init__(self) -> None:
                self.up_calls: list[Any] = []

            async def up(self, spec: Any, *, wait: bool = True) -> object:
                self.up_calls.append((spec, wait))
                return ComposeProjectPaths(
                    project_dir=Path("/tmp/awf-compose/ws_policy"),
                    compose_file=Path("/tmp/awf-compose/ws_policy/compose.yml"),
                )

        compose = _RecordingCompose()
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=ComposeStackLauncher(
                compose=compose,  # type: ignore[arg-type]
                agent_runtime_image="awf-agent-runtime:test",
            ),
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
                requested_profile={
                    "name": "restricted",
                    "security": {
                        "egress": {
                            "mode": "allowlist",
                            "allowlist": ["api.github.com"],
                        }
                    },
                },
            )
            await s.commit()
            ws_id = ws.id

        with pytest.raises(ProfileResolutionError):
            await provisioner.provision(ws_id)

        assert compose.up_calls == []
        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "profile_resolution_failure"
            assert reloaded.failure_message is not None
            assert len(reloaded.failure_message) <= 2000
            assert "security.egress.mode" in reloaded.failure_message
            failed_events = [
                event
                for event in reloaded.events
                if event.event_type == "workspace.state_changed"
                and event.new_state == WorkspaceStatus.failed.value
            ]
            assert failed_events[-1].reason_code == "PROFILE_RESOLUTION_FAILURE"
