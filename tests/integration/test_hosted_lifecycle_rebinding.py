"""Command authority is separate from committed boot/material authority."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.db.models import Workspace
from awf.hosted_node.models import Grant, Receipt
from awf.hosted_node.receiver import Receiver
from tests.integration.test_retained_work_acceptance import ACK, PREFIX, WS, fresh_bindings
from tests.integration.test_retained_work_acceptance import retained as retained
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.fixture(autouse=True, params=["same-physical-restart", "replacement"])
def boot_mode(authority, request, monkeypatch):
    if request.param == "same-physical-restart":
        start = Receiver.start

        async def restart(_config, factory):
            return await start(authority[0].receiver.config, factory)

        # Run the real Receiver.start, selecting the unchanged host configuration
        # rather than the replacement config constructed by the reusable fixture.
        monkeypatch.setattr(Receiver, "start", restart)
    return request.param


def control_body(action):
    return {
        "reason": "Fresh caller decision",
        "hosted_authorization": {"namespace": "tenant", "actor": "current-caller"},
        **({"directive": "Continue existing review"} if action == "guide" else {}),
    }


@pytest.mark.parametrize("retained", ["monitor", "human"], indirect=True)
@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_control_after_committed_recovery_uses_rebound_material(retained, action, boot_mode):
    client, runtime, factory, previous, _, root = retained
    bindings = await fresh_bindings(retained)
    assert runtime.receiver.identity.boot_epoch > previous.boot_epoch
    if boot_mode == "same-physical-restart":
        assert runtime.receiver.identity.incarnation == previous.incarnation
        assert runtime.receiver.identity.recovery_generation == previous.recovery_generation
    else:
        assert runtime.receiver.identity.incarnation != previous.incarnation
    denied = await client.post(
        f"/v1/workspaces/{WS}/{action}",
        json=control_body(action),
        headers={"Idempotency-Key": "control"},
    )
    assert denied.status_code in {403, 409}, denied.text
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["admission_closed"] is True
    compose = root / "retained-compose.yml"
    compose.write_text("services: {}\n")
    async with factory.begin() as session:
        row = await session.get(Workspace, WS)
        assert row.task_policy["hosted_node"]["credentials"] == bindings
        assert row.task_policy["hosted_recovery"]["disposition"] == "monitor"
        row.branch_name = row.remote_push_branch = "feature/existing"
        row.compose_project_name = "awf_retained"
        row.compose_file_path = str(compose)
        old_grants = list(
            await session.scalars(
                select(Grant)
                .join(Receipt, Receipt.grant == Grant.id)
                .where(
                    Receipt.binding["identity"]["boot_epoch"].as_integer() == previous.boot_epoch
                )
            )
        )
        assert old_grants and all(grant.revoked for grant in old_grants)
    response = await client.post(
        f"/v1/workspaces/{WS}/{action}",
        json=control_body(action),
        headers={"Idempotency-Key": "control"},
    )
    assert response.status_code == 200, response.text
    assert (await runtime.workspace(WS)).environment["GH_TOKEN"] == "fresh-github-key"


@pytest.mark.parametrize("retained", ["terminal"], indirect=True)
@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_idle_acceptance_does_not_rebind_terminal_history(retained, action):
    client, runtime, factory, previous, _, _ = retained
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert accepted.status_code == 200, accepted.text
    assert runtime.receiver.identity != previous
    response = await client.post(
        f"/v1/workspaces/{WS}/{action}",
        json=control_body(action),
        headers={"Idempotency-Key": "terminal"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "HOSTED_WORKSPACE_BINDING_MISMATCH"
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.status == "completed"
        assert (
            row.task_policy["hosted_node"]["credentials"]["agent"]["identity"]
            == previous.model_dump()
        )


@pytest.mark.parametrize("retained", ["monitor"], indirect=True)
@pytest.mark.parametrize("action", ["guide", "remonitor"])
@pytest.mark.parametrize("fault", ["expired", "revoked"])
async def test_fresh_command_never_replaces_missing_material_authority(
    retained, action, fault, monkeypatch
):
    from awf.hosted_node import runtime_authority
    from awf.hosted_node.schemas import Binding

    client, runtime, factory, _, _, _ = retained
    bindings = await fresh_bindings(retained)
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert accepted.status_code == 200, accepted.text
    if fault == "expired":

        class Expired(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz or UTC) + timedelta(hours=1)

        monkeypatch.setattr(runtime_authority, "datetime", Expired)
    else:
        binding = Binding.model_validate(
            {**bindings["agent"], "idempotency_key": "revocation", "fence": 2}
        )
        assert (await runtime.receiver.revoke(binding)).status == "revoked"
    response = await client.post(
        f"/v1/workspaces/{WS}/{action}",
        json=control_body(action),
        headers={"Idempotency-Key": "fresh-command"},
    )
    assert response.status_code == 503, response.text
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.task_policy["hosted_node"]["credentials"] == bindings
        assert row.pending_operator_hint is None
