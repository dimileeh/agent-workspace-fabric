"""Actual migrated remonitor association changes participate in version CAS."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from awf.db.models import Operation, Workspace
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_reauthorization import WS, install_pair, quiet_failed
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("status", ["monitoring_pr", "failed"])
@pytest.mark.parametrize("reauthorize", [False, True])
async def test_remonitor_version_tracks_only_committed_change_and_replay(
    authority, create_client, tmp_path, status, reauthorize
):
    runtime, value, factory = authority
    await quiet_failed(authority, create_client, tmp_path)
    async with factory.begin() as session:
        row = await session.get(Workspace, WS)
        row.status = status
        version = row.version
        assert row.monitor_claimed_by is row.execution_claimed_by is None
        assert not row.monitor_threads_addressed
        assert row.remote_push_branch and row.awaiting_human_since is None
    expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
    body = {"hosted_authorization": {"namespace": "tenant", "actor": "caller"}}
    replacement = expected
    if reauthorize:
        replacement = await install_pair(*create_client, expected, runtime.receiver.identity)
        body["hosted_reauthorization"] = {
            "expected_association": expected,
            "replacement_association": replacement,
        }
    client, headers = create_client
    headers = {**headers, "If-Match": str(version), "Idempotency-Key": "remonitor-version"}
    path = f"/v1/workspaces/{WS}/remonitor"
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    changed_version = version + int(reauthorize or status == "failed")
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.version == changed_version
        assert row.status == "monitoring_pr" and row.task_policy["hosted_node"] == replacement
    replay = await client.post(path, json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["operation_id"] == first.json()["operation_id"]
    if changed_version != version:
        stale = await client.post(
            path,
            json={"hosted_authorization": body["hosted_authorization"]},
            headers={**headers, "Idempotency-Key": "fresh-stale-version"},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["error_code"] == "VERSION_CONFLICT"
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.version == changed_version and row.task_policy["hosted_node"] == replacement
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.workspace_id == WS, Operation.type == "remonitor")
            )
            == 1
        )


@pytest.mark.parametrize("fault", ["preimage", "revoked"])
async def test_denied_remonitor_reauthorization_keeps_version_and_association(
    authority, create_client, tmp_path, fault
):
    runtime, value, factory = authority
    await quiet_failed(authority, create_client, tmp_path)
    async with factory.begin() as session:
        row = await session.get(Workspace, WS)
        row.status = "monitoring_pr"
        version = row.version
    expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
    replacement = await install_pair(*create_client, expected, runtime.receiver.identity)
    body = {
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
        "hosted_reauthorization": {
            "expected_association": deepcopy(expected),
            "replacement_association": replacement,
        },
    }
    if fault == "preimage":
        body["hosted_reauthorization"]["expected_association"]["credentials"]["agent"]["fence"] += 1
    else:
        await runtime.receiver.revoke(
            Binding.model_validate(
                {
                    **replacement["credentials"]["agent"],
                    "idempotency_key": "revoked-fresh",
                    "fence": 2,
                }
            )
        )
    result = await create_client[0].post(
        f"/v1/workspaces/{WS}/remonitor",
        json=body,
        headers={**create_client[1], "If-Match": str(version), "Idempotency-Key": "deny"},
    )
    assert result.status_code == (503 if fault == "revoked" else 409), result.text
    assert result.json()["error_code"] == (
        "PROTECTED_MATERIAL_UNAVAILABLE"
        if fault == "revoked"
        else "HOSTED_REAUTHORIZATION_PREIMAGE_MISMATCH"
    )
    async with factory() as session:
        row = await session.get(Workspace, WS)
        assert row.version == version and row.task_policy["hosted_node"] == expected
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.workspace_id == WS, Operation.type == "remonitor")
            )
            == 0
        )
