"""Original runtime anchors survive two-role renewal, with real HTTP/PG/materials.

The existing authority fixture seeds accepted host recovery; only external Docker
provider probing is synthetic. No worker execution or live recovery is claimed.
"""

import base64
import copy
import subprocess
from datetime import UTC, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings, get_settings
from awf.hosted_node import runtime_authority
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.schemas import Binding
from awf.service import provider_readiness
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.test_hosted_runtime_authority import authority as authority

from .test_preparation_renewal import PREFIX
from .test_preparation_renewal import clock as clock


@pytest.fixture
async def renewal_workspace(authority, clock, monkeypatch):
    runtime, value, factory = authority
    receiver = runtime.receiver
    monkeypatch.setattr(runtime_authority, "datetime", clock)

    def probe(args, **kwargs):
        assert args[0] == "docker"
        return subprocess.CompletedProcess(args, 0, "synthetic-cli", "")

    monkeypatch.setattr(provider_readiness, "_run_subprocess", probe)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver = receiver
    app.state.hosted_runtime = runtime
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=receiver.config
    )
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": runtime})
    headers = {"Authorization": "Bearer synthetic-controller", **receiver.identity.headers()}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as client:
        slots, preparations = {}, {}
        for role, binding in value["hosted"]["credentials"].items():
            binding.update(
                preparation_id=f"long-{role}",
                grant_id=f"long-{role}",
                idempotency_key=f"long-{role}",
            )
            binding["expires_at"] = (clock.now(UTC) + timedelta(minutes=10)).isoformat()
            preparation = {
                key: binding[key]
                for key in (
                    "namespace",
                    "workspace_id",
                    "preparation_id",
                    "purpose",
                    "target",
                )
            }
            preparation["expires_at"] = (clock.now(UTC) + timedelta(minutes=50)).isoformat()
            preparations[role] = preparation
            assert (
                await client.post(f"{PREFIX}/preparations", json=preparation)
            ).status_code == 200
            installed = await client.post(
                f"{PREFIX}/materials/install",
                json={
                    **binding,
                    "material": base64.b64encode(f"synthetic-{role}".encode()).decode(),
                },
            )
            assert installed.json()["status"] == "current", installed.text
            slots[role] = installed.json()["receipt_id"]
        result = await client.post(
            "/v1/workspaces", json=value, headers={"Idempotency-Key": "create-renewal"}
        )
        assert result.status_code == 202, result.text
        assert result.json()["workspace_id"] == value["hosted"]["workspace_id"]
        original = WorkspaceCreateRequest.model_validate(value).hosted.model_dump(mode="json")
        yield client, runtime, original, preparations, slots, clock


@pytest.mark.parametrize("denial", [None, "agent", "github", "renewal-anchor"])
async def test_both_roles_refresh_beyond_original_cap_without_runtime_reassociation(
    renewal_workspace, denial
):
    client, runtime, original, preparations, slots, clock = renewal_workspace
    start = clock.now(UTC)
    current = copy.deepcopy(original["credentials"])
    renewals = {}
    for minute in (9, 18, 27, 36, 45, 49):
        clock.instant = start + timedelta(minutes=minute)
        for role, binding in current.items():
            if minute == 49:
                request = {
                    "idempotency_key": f"renew-{role}",
                    "current": binding,
                    "expected_preparation_expires_at": preparations[role]["expires_at"],
                    "expires_at": (clock.now(UTC) + timedelta(minutes=50)).isoformat(),
                }
                response = await client.post(f"{PREFIX}/preparations/renew", json=request)
                assert response.status_code == 200, response.text
                renewals[role] = response.json()
            expiry = min(start + timedelta(minutes=50), clock.now(UTC) + timedelta(minutes=10))
            if minute == 49:
                expiry = clock.now(UTC) + timedelta(minutes=10)
            binding.update(
                idempotency_key=f"refresh-{role}-{minute}",
                revision=binding["revision"] + 1,
                fence=binding["fence"] + 1,
                expires_at=expiry.isoformat(),
            )
            response = await client.post(
                f"{PREFIX}/materials/refresh",
                json={
                    **binding,
                    "predecessor": slots[role],
                    "material": base64.b64encode(f"synthetic-{role}-{minute}".encode()).decode(),
                },
            )
            assert response.status_code == 200 and response.json()["status"] == "current", (
                response.text
            )
            slots[role] = response.json()["receipt_id"]
    clock.instant = start + timedelta(minutes=51)
    association, agent = await runtime.association(original["workspace_id"])
    assert association.model_dump(mode="json") == original
    if denial in {"agent", "github"}:
        revoked = await client.post(
            f"{PREFIX}/materials/revoke",
            json={
                **current[denial],
                "idempotency_key": f"revoke-{denial}",
                "fence": current[denial]["fence"] + 1,
            },
        )
        assert revoked.json()["status"] == "revoked", revoked.text
        with pytest.raises(MaterialDeniedError):
            await runtime.materials(association, agent, current=True)
    elif denial == "renewal-anchor":
        fake = Binding.model_validate({**current["agent"], "idempotency_key": "renew-agent"})
        with pytest.raises(MaterialDeniedError, match="IDEMPOTENCY_CONFLICT"):
            await runtime._current(fake)
    else:
        material = await runtime.materials(association, agent, current=True)
        assert material.environment == {
            "CODEX_API_KEY": "synthetic-agent-49",
            "GH_TOKEN": "synthetic-github-49",
            "GITHUB_TOKEN": "synthetic-github-49",
        }
        assert material.expires_at == start + timedelta(minutes=59)
        for role, anchor in association.credentials.model_dump().items():
            resolved = await runtime._current(Binding.model_validate(anchor))
            assert resolved.grant_id == original["credentials"][role]["grant_id"]
            assert resolved.preparation_id == original["credentials"][role]["preparation_id"]
            assert slots[role] != renewals[role]["receipt_id"]
    assert (await runtime.association(original["workspace_id"]))[0] == association
