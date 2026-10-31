"""Fresh association contracts on the actual current migrated PostgreSQL schema."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.db.models import Operation, Workspace
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from tests.integration import test_hosted_lifecycle_reauthorization as reauth
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_physical_history import (
    test_terminal_source_survives_accepted_physical_repair as physical_repair,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["retry", "adopt-pr", "guide", "remonitor"])
async def test_migrated_physical_replacement_and_two_later_wakes(
    authority, create_client, create_application, tmp_path, monkeypatch, action
):
    await physical_repair(
        authority, create_client, create_application, tmp_path, monkeypatch, 2, action
    )


@pytest.mark.parametrize("action", ["retry", "adopt-pr", "guide", "remonitor"])
async def test_migrated_two_wake_original_source_positive(
    authority, create_client, create_application, tmp_path, action
):
    if action in {"retry", "adopt-pr"}:
        await reauth.test_original_terminal_source_after_two_accepted_wakes(
            authority, create_client, create_application, tmp_path, action
        )
    else:
        await reauth.test_failed_control_fresh_install_atomic_rebind_and_replay(
            authority, create_client, create_application, tmp_path, action, 2
        )


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_expired_original_material_fresh_authorized_control_under_maintenance(
    authority, create_client, tmp_path, monkeypatch, action
):
    from awf.hosted_node import lifecycle_operations, receiver, runtime_authority

    runtime, value, factory = authority
    await reauth.quiet_failed(authority, create_client, tmp_path)
    expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
    replacement = await reauth.install_pair(
        *create_client, expected, runtime.receiver.identity, minutes=30
    )
    after_expiry = max(
        Binding.model_validate(binding).expires_at for binding in expected["credentials"].values()
    ) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return after_expiry.astimezone(tz or UTC)

    for module in (receiver, runtime_authority, lifecycle_operations):
        monkeypatch.setattr(module, "datetime", Clock)
    for binding in expected["credentials"].values():
        status = await runtime.receiver.status(Binding.model_validate(binding))
        assert status.status == "expired"
    async with factory.begin() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        gate.maintenance_generation = 7
    body = {
        "reason": "Current authorized resumption",
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
        "hosted_reauthorization": {
            "expected_association": expected,
            "replacement_association": replacement,
        },
        **({"directive": "Continue review"} if action == "guide" else {}),
    }
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "expired-original"}
    result = await client.post(f"/v1/workspaces/{reauth.WS}/{action}", json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert (await runtime.workspace(reauth.WS)).environment["GH_TOKEN"] == "fresh-github"
    async with factory() as session:
        row = await session.get(Workspace, reauth.WS)
        assert row.status == "monitoring_pr"
        assert row.task_policy["hosted_node"] == replacement
        assert (await session.get(NodeLifecycleRow, 1)).maintenance_generation == 7
        operation = await session.scalar(select(Operation).where(Operation.type == action))
        assert operation.payload["hosted_reauthorization"] == body["hosted_reauthorization"]
    for binding in expected["credentials"].values():
        assert (await runtime.receiver.status(Binding.model_validate(binding))).status == "expired"
