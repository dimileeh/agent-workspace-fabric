"""Receipt refresh must not invalidate an otherwise identical create replay."""

import base64
import copy
import subprocess

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.models import Workspace
from awf.db.repositories import ResourceReservationRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding, Refresh
from awf.service import provider_readiness
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.mark.parametrize("latest_receipt", [False, True])
async def test_same_create_replays_after_refresh_without_rebinding(
    authority, monkeypatch, latest_receipt
):
    authority, payload, factory = authority
    original = copy.deepcopy(payload)
    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = authority.receiver, authority
    settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=authority.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": authority})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "stable-create",
        **authority.receiver.identity.headers(),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/workspaces", json=payload, headers=headers)
        assert first.status_code == 202, first.text
        assert await authority.resource_limits("ws_cloud_stable") == (6, 16)
        for role, value in payload["hosted"]["credentials"].items():
            binding = Binding.model_validate(value)
            status = await authority.receiver.status(binding)
            updated = {**value, "revision": 2, "fence": 2, "idempotency_key": f"refresh-{role}"}
            refreshed = await authority.receiver.install(
                Refresh(
                    **updated,
                    predecessor=status.receipt_id,
                    material=base64.b64encode(f"synthetic-new-{role}".encode()).decode(),
                ),
                refresh=True,
            )
            assert refreshed.status == "current"
            payload["hosted"]["credentials"][role] = updated
        replay = payload if latest_receipt else original
        response = await client.post("/v1/workspaces", json=replay, headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()["workspace_id"] == first.json()["workspace_id"]
        async with factory() as session:
            workspace = await session.get(Workspace, "ws_cloud_stable")
            assert workspace.task_policy["hosted_node"] == HostedWorkspace.model_validate(
                original["hosted"]
            ).model_dump(mode="json")
        async with factory.begin() as session:
            await ResourceReservationRepository(session).release_active_for_workspace(
                "ws_cloud_stable"
            )
        with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_RESERVATION_REQUIRED"):
            await authority.resource_limits("ws_cloud_stable")
        await authority.receiver.revoke(
            Binding.model_validate(
                {
                    **payload["hosted"]["credentials"]["agent"],
                    "idempotency_key": "revoke-agent",
                    "fence": 3,
                }
            )
        )
        denied = await client.post("/v1/workspaces", json=replay, headers=headers)
        assert denied.status_code >= 400


@pytest.mark.parametrize(
    "field,value",
    [
        ("grant_id", "another-grant"),
        ("authorization_generation", 2),
        ("preparation_id", "another-preparation"),
        ("target", "/run/awf/secrets/other.api-key"),
    ],
)
def test_refresh_replay_never_rebinds_immutable_authority(field, value):
    from awf.hosted_node.runtime_schemas import association_matches
    from tests.unit.test_hosted_runtime_contract import hosted_create

    original = HostedWorkspace.model_validate(hosted_create()["hosted"])
    changed = original.model_copy(
        update={
            "credentials": original.credentials.model_copy(
                update={
                    "agent": original.credentials.agent.model_copy(update={field: value}),
                }
            ),
        }
    )
    assert not association_matches(original.model_dump(mode="json"), changed)
