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
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.fixture(params=["same-physical-restart", "replacement"])
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


@pytest.mark.usefixtures("boot_mode")
@pytest.mark.parametrize("retained", ["monitor", "human"], indirect=True)
@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_control_after_committed_recovery_uses_rebound_material(boot_mode, retained, action):
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


@pytest.mark.usefixtures("boot_mode")
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


@pytest.mark.usefixtures("boot_mode")
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


@pytest.mark.parametrize(
    ("action", "boots", "fault", "expected_error"),
    [
        ("remonitor", 2, None, None),
        ("guide", 2, None, None),
        ("guide", 0, None, None),
        ("remonitor", 0, "awake", "HOSTED_WORKSPACE_HISTORY_MISMATCH"),
        ("remonitor", 2, "awake", "HOSTED_WORKSPACE_BINDING_MISMATCH"),
        ("remonitor", 2, "owner", "HOSTED_WORKSPACE_BINDING_MISMATCH"),
        ("remonitor", 2, "namespace", "HOSTED_WORKSPACE_BINDING_MISMATCH"),
        ("guide", 2, "workspace", "HOSTED_WORKSPACE_BINDING_MISMATCH"),
        ("guide", 2, "receipt", "HOSTED_WORKSPACE_HISTORY_MISMATCH"),
        ("guide", 2, "missing_receipt", "HOSTED_WORKSPACE_HISTORY_MISMATCH"),
        ("remonitor", 2, "claim", "HOSTED_SOURCE_NOT_QUIESCENT"),
        ("remonitor", 2, "operation", "HOSTED_SOURCE_NOT_QUIESCENT"),
        ("guide", 2, "unaccepted", "NODE_ADMISSION_CLOSED"),
        ("remonitor", 2, "revoked_replacement", "PROTECTED_MATERIAL_UNAVAILABLE"),
        ("guide", 2, "old_replacement", "IDENTITY_MISMATCH"),
    ],
)
async def test_hosted_dormant_control_with_revoked_install_receipts(
    authority, create_application, create_client, tmp_path, action, boots, fault, expected_error
):
    from copy import deepcopy

    from awf.db.models import Operation
    from awf.hosted_node.runtime_schemas import HostedWorkspace
    from awf.hosted_node.schemas import Binding
    from tests.integration.test_hosted_lifecycle_parity import source_workspace
    from tests.integration.test_hosted_lifecycle_reauthorization import install_pair, wake

    await source_workspace(authority, create_client, tmp_path)
    runtime, value, factory = authority
    expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
    for role, binding in expected["credentials"].items():
        revoked = await runtime.receiver.revoke(
            Binding.model_validate({**binding, "idempotency_key": f"dormancy-{role}", "fence": 2})
        )
        assert revoked.status == "revoked"
    async with factory.begin() as session:
        row = await session.get(Workspace, WS)
        row.dormant_at = row.awaiting_human_since = datetime.now(UTC)
        for binding in expected["credentials"].values():
            receipt = await session.scalar(
                select(Receipt).where(Receipt.idempotency_key == binding["idempotency_key"])
            )
            assert receipt.operation == "install"
            if fault == "missing_receipt":
                await session.delete(receipt)
            else:
                receipt.state = "revoked"
                if fault == "receipt":
                    receipt.binding = {**receipt.binding, "fence": 99}

    try:
        if fault == "old_replacement":
            replacement = await install_pair(*create_client, expected, runtime.receiver.identity)
        for boot in range(boots):
            runtime = await wake(
                authority,
                create_application,
                create_client,
                accepted=fault != "unaccepted" or boot < boots - 1,
            )
        client, headers = create_client
        if fault == "unaccepted":
            # Fresh grants can be prepared on the current boot before Core admits execution.
            assert (
                runtime.receiver.identity.boot_epoch
                == expected["credentials"]["agent"]["identity"]["boot_epoch"] + 2
            )
        if fault != "old_replacement":
            replacement = await install_pair(client, headers, expected, runtime.receiver.identity)
        if fault == "revoked_replacement":
            revoked = await runtime.receiver.revoke(
                Binding.model_validate(
                    {
                        **replacement["credentials"]["agent"],
                        "idempotency_key": "replacement-revoke",
                        "fence": 2,
                    }
                )
            )
            assert revoked.status == "revoked"
        async with factory.begin() as session:
            row = await session.get(Workspace, WS)
            if fault == "awake":
                row.dormant_at = None
            elif fault in {"owner", "workspace"}:
                incorrect_association = deepcopy(expected)
                if fault == "workspace":
                    incorrect_association["workspace_id"] = "ws_foreign"
                for binding in incorrect_association["credentials"].values():
                    if fault == "workspace":
                        binding["workspace_id"] = "ws_foreign"
                    else:
                        binding["identity"]["owner"] = "different-node"
                row.task_policy = {**row.task_policy, "hosted_node": incorrect_association}
            elif fault == "claim":
                row.monitor_claimed_by = "monitor"
                row.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            elif fault == "operation":
                session.add(
                    Operation(workspace_id=WS, type="validate", status="pending", id="pending")
                )

        body = control_body(action)
        if fault == "namespace":
            body["hosted_authorization"]["namespace"] = "foreign"
        body["hosted_reauthorization"] = {
            "expected_association": expected,
            "replacement_association": replacement,
        }
        response = await client.post(
            f"/v1/workspaces/{WS}/{action}",
            json=body,
            headers={**headers, "Idempotency-Key": "dormant-rebind"},
        )
        if expected_error:
            assert response.status_code in {403, 409, 503}, response.text
            assert response.json()["error_code"] == expected_error
        else:
            assert response.status_code == 200, response.text
            async with factory() as session:
                row = await session.get(Workspace, WS)
                assert row.dormant_at is None
                assert row.task_policy["hosted_node"] == replacement
            assert (await runtime.workspace(WS)).environment["GH_TOKEN"] == "fresh-github"
    finally:
        if boots:
            await runtime.receiver.close()
