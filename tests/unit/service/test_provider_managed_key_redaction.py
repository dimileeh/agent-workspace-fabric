"""Neither the management key nor a minted key leaks anywhere across the lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from sqlalchemy import select

from awf.adapters import registry  # noqa: F401
from awf.adapters.base import AgentRunError
from awf.adapters.registry_api import get_adapter
from awf.common.agent_execution import execution_from_policy
from awf.common.commands import FakeCommandRunner
from awf.common.config import Settings, get_settings
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Operation, ProviderManagedKey, Workspace, WorkspaceEvent
from awf.db.repositories import WorkerHeartbeatRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.auth_mounts import ServiceAuthMountResolver
from awf.node.compose_manager import ComposeManager
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import WorkspaceProfile
from awf.service.config import resolve_service_settings, service_config_payload
from awf.service.doctor import _secret_values
from awf.service.provider_managed_key_reconciler import reconcile_openrouter_keys
from awf.service.provider_managed_keys import ManagedKeyBroker
from tests.shared.openrouter_management import FakeOpenRouterManagement

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("mock_docker_cli_probe")]

_MANAGEMENT = "sk-or-mgmt-REDACTION-SENTINEL-0123456789"
_PROFILE = {"name": "inline", "runtime": {"environment": {"TOOL_MODE": "safe"}}}


async def test_secrets_never_reach_stacks_argv_logs_or_rows(client, engine, monkeypatch, tmp_path):
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _MANAGEMENT)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-static-sentinel")
    get_settings.cache_clear()
    fake = FakeOpenRouterManagement()
    monkeypatch.setattr(
        "awf.api.routes.controls.management_client_from_settings",
        lambda _settings: fake.client(_MANAGEMENT),
    )
    factory = make_session_factory(engine)
    now = datetime.now(UTC)
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="w",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=3,
        )
        await session.commit()
    surfaces: list[str] = []

    with structlog.testing.capture_logs() as logs:
        created = await client.post(
            "/v1/workspaces",
            json={
                "repo": {"url": "https://github.com/example/project"},
                "task": {"title": "t", "prompt": "fix", "provider": "openrouter", "model": "m/x"},
                "workspace": {"profile": _PROFILE},
                "preflight": {"allow_unverified_model": True},
            },
        )
        assert created.status_code == 202, created.json()
        workspace_id = created.json()["workspace_id"]
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            selection = execution_from_policy(ws.task_policy)
            for step in ("provisioning", "ready", "running"):
                await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
            await session.commit()
        assert selection is not None and selection.managed_key

        # Rendered stack material for the managed selection.
        compose = ComposeManager(
            work_dir=tmp_path / "work",
            template_path=Path("docker/compose/workspace.base.yml.j2").resolve(),
        )
        launcher = ComposeStackLauncher(
            compose=compose,
            agent_runtime_image="pinned",
            auth_mount_resolver=ServiceAuthMountResolver(
                host_home=tmp_path / "home", work_dir=tmp_path / "auth"
            ),
        )
        spec, _, _ = await launcher._compose_spec(
            WorkspaceStackLaunchRequest(
                workspace_id=workspace_id,
                layout=WorktreeLayout(
                    mirror_path=tmp_path / "m.git",
                    worktree_path=tmp_path / "wt",
                    branch_name="awf/x",
                ),
                profile=WorkspaceProfile.model_validate(_PROFILE),
                execution=selection,
            ),
            build_companion_images=False,
        )
        surfaces.append(compose.render(spec).compose_file.read_text())
        surfaces.append(json.dumps(spec.agent_environment))

        # One managed invocation that exhausts its key, then the park.
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client(_MANAGEMENT))

        class _SpendingRunner(FakeCommandRunner):
            async def run(self, args, **kwargs):
                for key in fake.keys.values():
                    key["usage"] = key["limit"]
                return await super().run(args, **kwargs)

        runner = _SpendingRunner()
        runner.queue_result(returncode=1, stderr="402 Payment Required")
        adapter = get_adapter("codex", runner=runner, execution=selection, credential_broker=broker)
        with pytest.raises(AgentRunError) as raised:
            await adapter.run(
                compose_project="awf_x",
                compose_file=tmp_path / "compose.yml",
                prompt="fix",
                workspace_id=workspace_id,
            )
        assert raised.value.reason_code == "PROVIDER_BUDGET_EXHAUSTED"
        surfaces.append(str(raised.value))
        surfaces.append(json.dumps(raised.value.details, default=str))
        for call in runner.calls:
            surfaces.append(json.dumps(call.args))
            assert _MANAGEMENT not in json.dumps(call.env or {})
        # Positive control: the minted key does reach the exec env (and only it).
        assert fake.minted_values[0] in json.dumps(runner.calls[0].env)
        executor = WorkspaceExecutor(
            session_factory=factory,
            runner=runner,
            compose=compose,
            validation=None,
            pr_creator=None,
            config=ExecutorConfig(
                worktrees_root=tmp_path / "w", compose_projects_root=tmp_path / "c"
            ),
        )
        await executor._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message=str(raised.value),
            reason_code=raised.value.reason_code,
            details=raised.value.details,
        )

        # Operator raise, terminal cleanup and reconciliation.
        raised_budget = await client.post(
            f"/v1/workspaces/{workspace_id}/provider-budget",
            json={"usd": 25},
            headers={"Idempotency-Key": "redaction"},
        )
        assert raised_budget.status_code == 200, raised_budget.json()
        surfaces.append(raised_budget.text)
        await broker.acquire(workspace_id=workspace_id, selection=selection, purpose="agent")
        async with factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="test")
            await session.commit()
        await broker.finalize_workspace_keys(workspace_id)
        fake.add_key(f"awf-{workspace_id}-99")
        await reconcile_openrouter_keys(session_factory=factory, client=fake.client(_MANAGEMENT))
        surfaces.append((await client.get(f"/v1/workspaces/{workspace_id}")).text)

    surfaces.append(json.dumps(logs, default=str))
    async with factory() as session:
        for model in (ProviderManagedKey, WorkspaceEvent, Operation, Workspace):
            for row in (await session.execute(select(model))).scalars().all():
                surfaces.append(
                    json.dumps(
                        {c.name: getattr(row, c.name) for c in model.__table__.columns},
                        default=str,
                    )
                )
    service = resolve_service_settings(Settings(_env_file=None), environ={})
    surfaces.append(json.dumps(service_config_payload(service), default=str))
    get_settings.cache_clear()

    assert fake.minted_values, "the flow minted keys"
    assert _MANAGEMENT in _secret_values(service)
    combined = "\n".join(surfaces)
    for secret in (_MANAGEMENT, *fake.minted_values):
        assert secret not in combined
