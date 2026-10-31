"""Historical ownership is not credential authority; fresh controls are atomic."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from awf.db.models import Operation, ResourceReservation, TaskAttempt, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.lifecycle_schemas import HostObservation, RecoveryEvidence
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import SESSION_KEY, WorkspaceAuthority
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_parity import source_workspace
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client

WS = "ws_cloud_stable"
PREFIX = "/v1/hosted-node"


async def quiet_failed(authority, create_client, tmp_path):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    async with authority[2].begin() as session:
        row = await session.get(Workspace, WS)
        row.pr_url, row.pr_number = "https://github.com/example/repo/pull/42", 42
        row.branch_name = row.remote_push_branch = "feature/existing"
        compose = tmp_path / "compose.yml"
        row.compose_file_path = str(compose)
        row.compose_project_name = "awf_existing"
        await WorkspaceRepository(session).add_event(
            row,
            event_type="workspace.terminal_runtime_released",
            reason_code="TERMINAL_RUNTIME_RELEASED",
        )
        attempt = await session.scalar(select(TaskAttempt).where(TaskAttempt.workspace_id == WS))
        attempt.status = "failed"
        reservation = await session.scalar(
            select(ResourceReservation).where(ResourceReservation.workspace_id == WS)
        )
        reservation.released_at = datetime.now(UTC)


async def wake(authority, create_application, create_client, *, accepted=True):
    old, _, factory = authority
    previous = create_application[0].state.hosted_receiver
    receiver = await Receiver.start(previous.config, factory)
    await previous.close()
    await receiver.identity_status("current-identity")

    async def proof(identity, prior, receipt):
        return RecoveryEvidence(
            identity, prior, "disk", "db", receipt, datetime.now(UTC) + timedelta(seconds=30)
        )

    async def host(identity):
        return HostObservation(identity, datetime.now(UTC) + timedelta(seconds=30), True, True)

    receiver.lifecycle.verify_recovery = proof
    receiver.lifecycle.observe_host = host
    receiver.lifecycle.observe_runtime = AsyncMock(return_value=True)
    runtime = WorkspaceAuthority(receiver)
    app, headers = create_application
    app.state.hosted_receiver, app.state.hosted_runtime = receiver, runtime
    factory.configure(info={**factory.kw.get("info", {}), SESSION_KEY: runtime})
    headers.update(receiver.identity.headers())
    if accepted:
        response = await create_client[0].post(
            PREFIX + "/lifecycle/recovery/acknowledge",
            json={"idempotency_key": "wake-accept", "receipt_ref": "trusted-proof"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    return runtime


async def install_pair(
    client, headers, expected, identity, *, workspace_id=WS, label="fresh", minutes=10
):
    replacement = deepcopy(expected)
    replacement["workspace_id"] = workspace_id
    for role, binding in replacement["credentials"].items():
        binding.update(
            workspace_id=workspace_id,
            identity=identity.model_dump(),
            expires_at=(datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
        )
        for key in ("preparation_id", "grant_id", "idempotency_key"):
            binding[key] = f"{label}-{role}-{key}"
        prep = {
            key: binding[key]
            for key in (
                "namespace",
                "workspace_id",
                "preparation_id",
                "purpose",
                "target",
                "expires_at",
            )
        }
        response = await client.post(PREFIX + "/preparations", json=prep, headers=headers)
        assert response.status_code == 200, response.text
        response = await client.post(
            PREFIX + "/materials/install",
            json={**binding, "material": base64.b64encode(f"{label}-{role}".encode()).decode()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "current"
    return HostedWorkspace.model_validate(replacement).model_dump(mode="json")


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
async def test_original_terminal_source_after_two_accepted_wakes(
    authority, create_client, create_application, tmp_path, action
):
    await quiet_failed(authority, create_client, tmp_path)
    before = HostedWorkspace.model_validate(authority[1]["hosted"]).model_dump(mode="json")
    runtime = None
    try:
        for _ in range(2):
            runtime = await wake(authority, create_application, create_client)
        client, headers = create_client
        target = await install_pair(
            client, headers, before, runtime.receiver.identity, workspace_id="ws_fresh_target"
        )
        if action == "retry":
            path, body = f"/v1/workspaces/{WS}/retry", {"hosted": target}
            # This case uses a terminal source without a retained PR for retry;
            # scoped adoption below proves the retained PR identity path.
            async with authority[2].begin() as session:
                row = await session.get(Workspace, WS)
                row.pr_url = row.pr_number = None
        else:
            create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
                return_value=_metadata(number=42)
            )
            path, body = (
                "/v1/workspaces/adopt-pr",
                {
                    "pr_url": "https://github.com/example/repo/pull/42",
                    "repo_url": "https://github.com/example/repo.git",
                    "source_workspace_id": WS,
                    "hosted": target,
                },
            )
        headers = {**headers, "Idempotency-Key": "after-two-wakes"}
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 202, response.text
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        key = "new_workspace_id" if action == "retry" else "workspace_id"
        assert replay.json()[key] == response.json()[key] == "ws_fresh_target"
        async with authority[2]() as session:
            source = await session.get(Workspace, WS)
            successor = await session.get(Workspace, "ws_fresh_target")
            assert source.task_policy["hosted_node"] == before
            assert successor.task_policy["hosted_node"] == target
        assert (await runtime.workspace("ws_fresh_target")).environment[
            "GH_TOKEN"
        ] == "fresh-github"
    finally:
        if runtime is not None:
            await runtime.receiver.close()


@pytest.mark.parametrize("action", ["guide", "remonitor"])
@pytest.mark.parametrize("wakes", [0, 2])
async def test_failed_control_fresh_install_atomic_rebind_and_replay(
    authority, create_client, create_application, tmp_path, action, wakes
):
    await quiet_failed(authority, create_client, tmp_path)
    expected = HostedWorkspace.model_validate(authority[1]["hosted"]).model_dump(mode="json")
    runtime = authority[0]
    try:
        for _ in range(wakes):
            runtime = await wake(authority, create_application, create_client)
        client, headers = create_client
        replacement = await install_pair(client, headers, expected, runtime.receiver.identity)
        if not wakes:
            # Actual revocation cleans the original grant/files, without denying
            # fresh independently authorized grants for the same workspace.
            for role, binding in expected["credentials"].items():
                revoked = await runtime.receiver.revoke(
                    Binding.model_validate(
                        {
                            **binding,
                            "idempotency_key": f"old-revoke-{role}",
                            "fence": 2,
                        }
                    )
                )
                assert revoked.status == "revoked"
        body = {
            "reason": "Authorized existing PR resumption",
            "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
            "hosted_reauthorization": {
                "expected_association": expected,
                "replacement_association": replacement,
            },
            **({"directive": "Continue existing review"} if action == "guide" else {}),
        }
        headers = {**headers, "Idempotency-Key": "reauthorize"}
        path = f"/v1/workspaces/{WS}/{action}"
        first = await client.post(path, json=body, headers=headers)
        assert first.status_code == 200, first.text
        replay = await client.post(path, json=body, headers=headers)
        assert replay.status_code == 200, replay.text
        assert first.json()["operation_id"] == replay.json()["operation_id"]
        async with authority[2]() as session:
            row = await session.get(Workspace, WS)
            assert row.status == "monitoring_pr"
            assert row.task_policy["hosted_node"] == replacement
            assert (
                await session.scalar(
                    select(func.count()).select_from(Operation).where(Operation.type == action)
                )
                == 1
            )
        assert (await runtime.workspace(WS)).environment["GH_TOKEN"] == "fresh-github"
        changed = await client.post(
            path, json=body, headers={**headers, "Idempotency-Key": "different"}
        )
        assert changed.status_code == 409, changed.text
    finally:
        if wakes:
            await runtime.receiver.close()
