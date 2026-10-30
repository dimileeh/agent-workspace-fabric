"""Actual protected credentials and ASGI create under a maintenance hold."""

import asyncio
import base64
import subprocess
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings, get_settings
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_authority import SESSION_KEY
from awf.hosted_node.schemas import Binding
from tests.conftest import _ok_workspace_admission_disk_check
from tests.integration.test_node_maintenance import maintenance
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace


async def test_hold_preserves_material_consumers_refresh_renew_revoke_and_read(bound_workspace):
    runtime, value, factory = bound_workspace
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    factory.configure(info={**factory.kw.get("info", {}), SESSION_KEY: runtime})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        **runtime.receiver.identity.headers(),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://core.test", headers=headers
    ) as client:
        assert (await maintenance(client)).status_code == 200
        # Both worker readiness and existing workspace credential consumers use
        # this shared check; neither must confuse an admission hold with drain.
        await runtime.admission()
        await runtime.workspace("ws_cloud_stable")
        denied = await client.post("/v1/workspaces", json=value)
        assert denied.status_code == 409, denied.text
        assert denied.json()["error_code"] == "NODE_MAINTENANCE_HELD"
        with pytest.raises(MaterialDeniedError, match="NODE_MAINTENANCE_HELD"):
            await runtime.admit(WorkspaceCreateRequest.model_validate(value))
        binding = Binding.model_validate(value["hosted"]["credentials"]["agent"])
        receipt = await runtime.receiver.status(binding)
        renewed_expiry = binding.expires_at + timedelta(seconds=30)
        response = await client.post(
            "/v1/hosted-node/preparations/renew",
            json={
                "idempotency_key": "maintenance-renewal",
                "current": binding.model_dump(mode="json"),
                "expected_preparation_expires_at": binding.expires_at.isoformat(),
                "expires_at": renewed_expiry.isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        successor = binding.model_copy(
            update={
                "idempotency_key": "maintenance-refresh",
                "revision": 2,
                "fence": 2,
                "expires_at": renewed_expiry,
            }
        )
        response = await client.post(
            "/v1/hosted-node/materials/refresh",
            json={
                **successor.model_dump(mode="json"),
                "predecessor": receipt.receipt_id,
                "material": base64.b64encode(b"synthetic-renewed-key").decode(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "current"
        assert (await runtime.workspace("ws_cloud_stable")).environment[
            "CODEX_API_KEY"
        ] == "synthetic-renewed-key"
        response = await client.post(
            "/v1/hosted-node/materials/revoke",
            json=successor.model_copy(
                update={
                    "idempotency_key": "maintenance-revoke",
                    "fence": 3,
                }
            ).model_dump(mode="json"),
        )
        assert response.status_code == 200 and response.json()["status"] == "revoked"
        with pytest.raises(MaterialDeniedError):
            await runtime.workspace("ws_cloud_stable")
        assert (
            await client.get(
                "/v1/workspaces/ws_cloud_stable",
                headers={"Authorization": "Bearer synthetic-operator"},
            )
        ).status_code == 200


async def test_hold_winning_after_http_preflight_returns_structured_denial(authority, monkeypatch):
    from awf.service import provider_readiness

    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )
    runtime, value, factory = authority
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), SESSION_KEY: runtime})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        **runtime.receiver.identity.headers(),
    }
    entered, resume = asyncio.Event(), asyncio.Event()
    original = WorkspaceRepository.find_active_owned_path_overlaps

    async def pause(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(WorkspaceRepository, "find_active_owned_path_overlaps", pause)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://core.test", headers=headers
    ) as client:
        pending = asyncio.create_task(client.post("/v1/workspaces", json=value))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await maintenance(client)).status_code == 200
        finally:
            resume.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "NODE_MAINTENANCE_HELD"
        async with factory() as session:
            assert await session.get(Workspace, "ws_cloud_stable") is None
        assert (await maintenance(client, "release")).status_code == 200
        accepted = await client.post("/v1/workspaces", json=value)
        assert accepted.status_code == 202, accepted.text
