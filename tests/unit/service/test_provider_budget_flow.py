"""End to end: create → mint → heartbeat → 402 → park → raise → resume → finalize → spend."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from awf.adapters import registry  # noqa: F401
from awf.adapters.base import AgentRunError
from awf.adapters.registry_api import get_adapter
from awf.common.agent_execution import execution_from_policy
from awf.common.commands import FakeCommandRunner
from awf.common.config import get_settings
from awf.control.executor import WorkspaceExecutor
from awf.control.executor.config import ExecutorConfig
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import (
    ProviderManagedKeyRepository,
    WorkerHeartbeatRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.provider_managed_keys import ManagedKeyBroker
from tests.shared.openrouter_management import FakeOpenRouterManagement

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("mock_docker_cli_probe")]


async def test_managed_budget_flow(client, engine, monkeypatch, tmp_path):
    monkeypatch.setenv("AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "sk-or-mgmt-flow")
    get_settings.cache_clear()
    fake = FakeOpenRouterManagement()
    monkeypatch.setattr(
        "awf.api.routes.controls.management_client_from_settings", lambda _s: fake.client()
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

    # 1. Create with a 2 USD budget.
    created = await client.post(
        "/v1/workspaces",
        json={
            "repo": {"url": "https://github.com/example/project"},
            "task": {
                "title": "t",
                "prompt": "fix",
                "provider": "openrouter",
                "model": "m/x",
                "provider_budget_usd": 2,
            },
            "workspace": {"profile": {"name": "inline"}},
            "preflight": {"allow_unverified_model": True},
        },
    )
    assert created.status_code == 202, created.json()
    ws_id = created.json()["workspace_id"]
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        selection = execution_from_policy(ws.task_policy)
        for step in ("provisioning", "ready", "running"):
            await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
        await session.commit()
    broker = ManagedKeyBroker(session_factory=factory, client=fake.client())

    # 2-3. A successful invocation mints the full budget; usage is read and the key deleted.
    lease = await broker.acquire(workspace_id=ws_id, selection=selection, purpose="agent")
    assert fake.calls[-1][2]["limit"] == 2.0
    fake.set_usage(lease.key_hash, 1.2)
    await lease.heartbeat()
    await lease.release(failed=False)

    # 4-5. The next invocation spends its key's whole limit and hits HTTP 402; the
    # authoritative key read confirms it and the executor sink parks the workspace.
    class _SpendingRunner(FakeCommandRunner):
        async def run(self, args, **kwargs):
            for key in fake.keys.values():
                key["usage"] = key["limit"]
            return await super().run(args, **kwargs)

    runner = _SpendingRunner()
    runner.queue_result(returncode=1, stderr="PROVIDER_HTTP_402")
    adapter = get_adapter("codex", runner=runner, execution=selection, credential_broker=broker)
    with pytest.raises(AgentRunError) as raised:
        await adapter.run(
            compose_project="awf_x",
            compose_file=tmp_path / "compose.yml",
            prompt="fix",
            workspace_id=ws_id,
        )
    assert fake.calls[-3][2]["limit"] == 0.8  # minted with the remaining budget
    executor = WorkspaceExecutor(
        session_factory=factory,
        runner=runner,
        compose=None,
        validation=None,
        pr_creator=None,
        config=ExecutorConfig(worktrees_root=tmp_path / "w", compose_projects_root=tmp_path / "c"),
    )
    await executor._mark_failed(
        workspace_id=ws_id,
        from_status=WorkspaceStatus.running,
        failure_reason=FailureReason.agent_failure,
        message=str(raised.value),
        reason_code=raised.value.reason_code,
        details=raised.value.details,
    )
    parked = (await client.get(f"/v1/workspaces/{ws_id}")).json()
    assert parked["status"] == "blocked"
    assert parked["block_state"]["block_reason_code"] == "PROVIDER_BUDGET_EXHAUSTED"
    assert parked["block_state"]["block_type"] == "provider_budget"

    # 6-7. The operator raises the budget; the blocked resume is armed.
    raised_budget = await client.post(
        f"/v1/workspaces/{ws_id}/provider-budget",
        json={"usd": 5},
        headers={"Idempotency-Key": "flow"},
    )
    assert raised_budget.status_code == 200, raised_budget.json()
    async with factory() as session:
        assert ws_id in await WorkspaceRepository(session).list_resumable_blocked_ids(limit=5)

    # 8. The next acquire mints the new remaining budget (5 - 2.0 spent).
    resumed = await broker.acquire(workspace_id=ws_id, selection=selection, purpose="agent")
    assert fake.calls[-1][2]["limit"] == 3.0
    fake.set_usage(resumed.key_hash, 0.3)

    # 9. Terminal finalize: final read, persist, delete.
    assert await broker.finalize_workspace_keys(ws_id) == {"finalized": 1, "failed": 0}
    assert fake.keys == {}
    async with factory() as session:
        rows = await ProviderManagedKeyRepository(session).for_workspace(ws_id)
    assert [r.status for r in rows] == ["deleted", "deleted", "deleted"]

    # 10. Summed exact spend on every surface.
    show = (await client.get(f"/v1/workspaces/{ws_id}")).json()
    listed = next(w for w in (await client.get("/v1/workspaces")).json() if w["id"] == ws_id)
    overview = (await client.get("/v1/workspaces/overview")).json()["items"][0]
    get_settings.cache_clear()
    for item in (show, listed, overview):
        assert item["provider_spend"]["spend_usd"] == float(Decimal("2.3"))
        assert item["provider_spend"]["budget_usd"] == 5.0
        assert item["provider_spend"]["remaining_usd"] == 2.7
        assert item["llm_usage"]["source"] == "openrouter_key_usage"
