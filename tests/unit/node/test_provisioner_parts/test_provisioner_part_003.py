"""Provisioner failure-path coverage split from the main provisioner part."""

from __future__ import annotations

import asyncio
import json
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
from awf.node.compose_manager import (
    ComposeManager,
    ComposeOperationError,
    ComposeProjectPaths,
)
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.node.stack_launcher import ComposeStackLauncher
from awf.profiles.resolver import ProfileResolutionError
from tests.postgres import postgres_test_engine

_COMPOSE_TEMPLATE = (
    Path(__file__).resolve().parents[4] / "docker" / "compose" / "workspace.base.yml.j2"
)


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


class _FailingComposeLauncher:
    """Stack launcher that fails exactly like a companion healthcheck timeout."""

    async def launch(self, request: Any) -> object:
        del request
        raise ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="dependency failed to start: container backend is unhealthy",
            reason_code="COMPOSE_COMMAND_FAILED",
        )


class _FakeDiagnostics:
    """Best-effort capturer stub returning a fixed already-redacted payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def capture_companion_diagnostics(
        self,
        *,
        project_name: str,
        workspace_id: str,
        tail_lines: int = 200,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "project_name": project_name,
                "workspace_id": workspace_id,
                "tail_lines": tail_lines,
            }
        )
        return self.payload


async def _create_failing_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> str:
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
        return ws.id


def _latest_failed_event(events: list[Any]) -> Any:
    failed = [
        event
        for event in events
        if event.event_type == "workspace.state_changed"
        and event.new_state == WorkspaceStatus.failed.value
    ]
    assert failed, "expected a state_changed -> failed event"
    return failed[-1]


class TestServiceStartupDiagnostics:
    @pytest.mark.unit
    async def test_failure_event_payload_carries_captured_diagnostics(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        diagnostics = {
            "schema": "service_startup_diagnostics.v1",
            "containers_inspected": 1,
            "unhealthy_companions": [{"service": "backend", "logs": ["boot failed"]}],
        }
        capturer = _FakeDiagnostics(diagnostics)
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=capturer,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.reason_code == "SERVICE_STARTUP_FAILURE"
            assert failed_event.payload == diagnostics
        # Capturer addressed the workspace's compose project with the configured tail.
        assert capturer.calls == [
            {
                "project_name": f"awf_{ws_id}",
                "workspace_id": ws_id,
                "tail_lines": 200,
            }
        ]

    @pytest.mark.unit
    async def test_capture_runs_before_mark_failed_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        order: list[str] = []

        class _OrderingDiagnostics:
            async def capture_companion_diagnostics(
                self, *, project_name: str, workspace_id: str, tail_lines: int = 200
            ) -> dict[str, Any]:
                del project_name, workspace_id, tail_lines
                order.append("capture")
                return {"schema": "service_startup_diagnostics.v1", "unhealthy_companions": []}

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=_OrderingDiagnostics(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        original_mark_failed = provisioner._mark_failed

        async def _record_mark_failed(**kwargs: Any) -> None:
            order.append("mark_failed")
            await original_mark_failed(**kwargs)

        monkeypatch.setattr(provisioner, "_mark_failed", _record_mark_failed)
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        assert order == ["capture", "mark_failed"]

    @pytest.mark.unit
    async def test_capture_failure_does_not_mask_original_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        class _RaisingDiagnostics:
            async def capture_companion_diagnostics(
                self, *, project_name: str, workspace_id: str, tail_lines: int = 200
            ) -> dict[str, Any]:
                del project_name, workspace_id, tail_lines
                raise ComposeOperationError(
                    operation="logs",
                    returncode=124,
                    stdout="",
                    stderr="docker logs timed out",
                    reason_code="DOCKER_COMMAND_TIMEOUT",
                )

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=_RaisingDiagnostics(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError) as exc:
            await provisioner.provision(ws_id)

        # The ORIGINAL compose failure propagates, not the capturer's error.
        assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.reason_code == "SERVICE_STARTUP_FAILURE"
            assert failed_event.payload is not None
            assert "companion_logs_capture_error" in failed_event.payload

    @pytest.mark.unit
    async def test_unexpected_capture_error_does_not_mask_original_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        # A capturer that raises something OTHER than ComposeOperationError
        # (e.g. a wiring/signature bug surfacing as TypeError) must still be
        # swallowed: the capture is best-effort and runs inside the
        # ``except ComposeOperationError`` handler, so an escaping error would
        # skip ``_mark_failed`` and mask the original stack-launch failure.
        class _BuggyDiagnostics:
            async def capture_companion_diagnostics(
                self, *, project_name: str, workspace_id: str, tail_lines: int = 200
            ) -> dict[str, Any]:
                del project_name, workspace_id, tail_lines
                raise TypeError("capturer wired with a stale signature")

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=_BuggyDiagnostics(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError) as exc:
            await provisioner.provision(ws_id)

        # The ORIGINAL compose failure propagates, not the capturer's TypeError.
        assert exc.value.reason_code == "COMPOSE_COMMAND_FAILED"

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            # ``_mark_failed`` still ran despite the capturer blowing up.
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.reason_code == "SERVICE_STARTUP_FAILURE"
            assert failed_event.payload is not None
            # The capture error is recorded under the fallback reason code so the
            # marker is still present for operators. It uses the same uniform
            # ``dict[str, str]`` shape as the compose-manager capture markers.
            marker = failed_event.payload["companion_logs_capture_error"]
            assert isinstance(marker, dict)
            assert marker["_top_level"].startswith("CAPTURE_FAILED:")

    @pytest.mark.unit
    async def test_cancellation_during_diagnostics_capture_still_marks_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        # Cancellation is a BaseException, so it escapes the capturer's broad
        # ``except Exception`` guard. If the diagnostics await is interrupted by
        # task cancellation (e.g. during shutdown), ``_mark_failed`` must still
        # run — otherwise the workspace stays ``provisioning`` and the secret
        # lease issued before stack launch is never revoked. ``_mark_failed``
        # runs in a ``finally`` so the failure path is finalized even though the
        # cancellation itself still propagates (it is never swallowed).
        raw_ref = "provider/path/not-a-token-value"

        class _CancellingDiagnostics:
            async def capture_companion_diagnostics(
                self, *, project_name: str, workspace_id: str, tail_lines: int = 200
            ) -> dict[str, Any]:
                del project_name, workspace_id, tail_lines
                raise asyncio.CancelledError

        profile = {
            "name": "cancelled-secrets",
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
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=_CancellingDiagnostics(),
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

        # The cancellation still propagates out of ``provision`` — never swallowed.
        with pytest.raises(asyncio.CancelledError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            # ``_mark_failed`` ran in the ``finally`` despite the cancellation.
            assert reloaded.status == WorkspaceStatus.failed.value
            assert reloaded.failure_reason == "service_startup_failure"
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.reason_code == "SERVICE_STARTUP_FAILURE"
            # The lease issued before stack launch is revoked through the normal
            # failure path rather than leaking.
            leases = await SecretLeaseRepository(s).list_for_workspace(ws_id)
            assert len(leases) == 1
            assert leases[0].status == "revoked"
            assert leases[0].revoke_reason_code == "PROVISIONING_FAILED"

    @pytest.mark.unit
    async def test_no_capturer_keeps_failure_event_payload_null(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.reason_code == "SERVICE_STARTUP_FAILURE"
            assert failed_event.payload is None

    @pytest.mark.unit
    async def test_end_to_end_real_capturer_redacts_planted_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        session_factory: async_sessionmaker[AsyncSession],
        git_manager: GitManager,
        origin_repo: Path,
    ) -> None:
        secret = "S3CRETPW_in_companion_logs"
        compose = ComposeManager(
            work_dir=tmp_path / "compose-work",
            template_path=_COMPOSE_TEMPLATE,
        )
        inspect_json = json.dumps(
            [
                {
                    "Id": "c_backend",
                    "Config": {
                        "Labels": {"com.docker.compose.service": "backend"},
                        "Healthcheck": {"Test": ["CMD-SHELL", "curl -f http://localhost:8000"]},
                    },
                    "State": {
                        "Status": "running",
                        "ExitCode": 0,
                        "Health": {"Status": "unhealthy", "Log": []},
                    },
                }
            ]
        )
        companion_logs = f"connecting to postgres://appuser:{secret}@db/awf\n"

        async def _fake_resource_ids(args: list[str], *, operation: str) -> list[str]:
            del args, operation
            return ["c_backend"]

        async def _fake_capture(
            args: list[str], *, operation: str, combine_stderr: bool = False
        ) -> str:
            del operation, combine_stderr
            if args[0] == "inspect":
                return inspect_json
            if args[0] == "logs":
                return companion_logs
            raise AssertionError(f"unexpected docker args: {args}")

        monkeypatch.setattr(compose, "_docker_resource_ids", _fake_resource_ids)
        monkeypatch.setattr(compose, "_docker_capture", _fake_capture)

        provisioner = Provisioner(
            session_factory=session_factory,
            git=git_manager,
            stack_launcher=_FailingComposeLauncher(),
            service_diagnostics=compose,
            config=ProvisionerConfig(node_id="test-node-01"),
        )
        ws_id = await _create_failing_workspace(session_factory, origin_repo)

        with pytest.raises(ComposeOperationError):
            await provisioner.provision(ws_id)

        async with session_factory() as s:
            reloaded = await WorkspaceRepository(s).get(ws_id)
            assert reloaded is not None
            failed_event = _latest_failed_event(reloaded.events)
            assert failed_event.payload is not None
            blob = json.dumps(failed_event.payload)
            assert secret not in blob
            assert "[redacted]" in blob


class TestProvisionerConfigValidation:
    """``ProvisionerConfig`` mirrors the ``gt=0`` guard pydantic Settings enforces."""

    @pytest.mark.unit
    def test_default_tail_lines_is_accepted(self) -> None:
        config = ProvisionerConfig(node_id="test-node-01")
        assert config.service_startup_log_tail_lines > 0

    @pytest.mark.unit
    def test_positive_tail_lines_is_accepted(self) -> None:
        config = ProvisionerConfig(node_id="test-node-01", service_startup_log_tail_lines=1)
        assert config.service_startup_log_tail_lines == 1

    @pytest.mark.unit
    @pytest.mark.parametrize("bad_value", [0, -1])
    def test_non_positive_tail_lines_is_rejected(self, bad_value: int) -> None:
        with pytest.raises(ValueError, match="service_startup_log_tail_lines must be > 0"):
            ProvisionerConfig(node_id="test-node-01", service_startup_log_tail_lines=bad_value)
