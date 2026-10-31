"""Successor authority, replay and transaction edges on the actual API."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Operation, ResourceReservation, Workspace
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.schemas import Binding
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


async def adoption(create_application, authority, *, source=None):
    create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
        return_value=replace(
            _metadata(number=42),
            base_ref="main",
            head_repo_slug="example/repo",
            url="https://github.com/example/repo/pull/42",
        )
    )
    return {
        "pr_url": "https://github.com/example/repo/pull/42",
        "hosted": await fresh_association(authority[0], authority[1]),
        **({"source_workspace_id": source} if source else {}),
    }


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
async def test_successor_placement_is_bound_to_actual_owner(
    authority, create_client, create_application, tmp_path, action
):
    client, headers = create_client
    if action == "retry":
        await source_workspace(authority, create_client, tmp_path, status="failed")
        body = {"hosted": await fresh_association(authority[0], authority[1])}
        path = "/v1/workspaces/ws_cloud_stable/retry"
    else:
        body = await adoption(create_application, authority)
        path = "/v1/workspaces/adopt-pr"
    response = await client.post(
        path, json=body, headers={**headers, "Idempotency-Key": "placement"}
    )
    assert response.status_code == 202, response.text
    async with authority[2]() as session:
        reservation = await session.scalar(
            select(ResourceReservation).where(ResourceReservation.workspace_id == "ws_successor")
        )
        assert reservation.node_id == authority[0].receiver.identity.owner


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
@pytest.mark.parametrize("change", ["namespace", "destination", "credentials"])
async def test_successor_replay_cannot_change_association(
    authority, create_client, create_application, tmp_path, action, change
):
    client, headers = create_client
    if action == "retry":
        await source_workspace(authority, create_client, tmp_path, status="failed")
        body = {"hosted": await fresh_association(authority[0], authority[1])}
        path = "/v1/workspaces/ws_cloud_stable/retry"
    else:
        body = await adoption(create_application, authority)
        path = "/v1/workspaces/adopt-pr"
    headers = {**headers, "Idempotency-Key": "replay"}
    assert (await client.post(path, json=body, headers=headers)).status_code == 202
    target = "ws_different" if change == "destination" else "ws_successor"
    value = deepcopy(authority[1])
    # Fresh, valid material is not permission to rebind the durable winner.
    if change == "credentials":
        target = "ws_successor"
        for role in value["hosted"]["credentials"]:
            value["hosted"]["credentials"][role]["authorization_generation"] = 2
    changed = (
        await fresh_association(
            authority[0], value, target, "foreign" if change == "namespace" else "tenant"
        )
        if change != "credentials"
        else deepcopy(body["hosted"])
    )
    if change == "credentials":
        from tests.unit.test_hosted_runtime_review_create import alternate_credentials

        changed = (await alternate_credentials(authority[0], {"hosted": body["hosted"]}))["hosted"]
    body["hosted"] = changed
    denied = await client.post(path, json=body, headers=headers)
    assert denied.status_code in {403, 409}, denied.text
    async with authority[2]() as session:
        assert await session.get(Workspace, "ws_different") is None


@pytest.mark.parametrize("changed_source", [False, True])
async def test_scoped_adoption_source_replay_binding(
    authority, create_client, create_application, tmp_path, changed_source
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    body = await adoption(create_application, authority, source="ws_cloud_stable")
    client, headers = create_client
    response = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    assert response.status_code == 202, response.text
    if changed_source:
        body.pop("source_workspace_id")
    replay = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    assert replay.status_code == (409 if changed_source else 202), replay.text


@pytest.mark.parametrize("edge", ["maintenance", "revoke"])
async def test_authority_changes_during_material_read_deny_commit(
    authority, create_client, tmp_path, monkeypatch, edge
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    association = await fresh_association(runtime, value)
    original = runtime.materials

    async def changed(*args, **kwargs):
        material = await original(*args, **kwargs)
        if edge == "maintenance":
            async with factory.begin() as session:
                gate = await locked(session, runtime.receiver.identity)
                gate.maintenance_generation = 8
        else:
            binding = association["credentials"]["agent"]
            await runtime.receiver.revoke(
                Binding.model_validate({**binding, "idempotency_key": "revoked", "fence": 2})
            )
        return material

    monkeypatch.setattr(runtime, "materials", changed)
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces/ws_cloud_stable/retry",
        json={"hosted": association},
        headers={**headers, "Idempotency-Key": "race"},
    )
    assert response.status_code in {409, 503}, response.text
    async with factory() as session:
        assert await session.get(Workspace, "ws_successor") is None


async def test_parallel_retry_same_key_one_successor(authority, create_client, tmp_path):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    association = await fresh_association(authority[0], authority[1])
    client, headers = create_client

    async def retry():
        return await client.post(
            "/v1/workspaces/ws_cloud_stable/retry",
            json={"hosted": association},
            headers={**headers, "Idempotency-Key": "parallel"},
        )

    responses = await asyncio.gather(retry(), retry())
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    async with authority[2]() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Operation).where(Operation.type == "retry")
            )
            == 1
        )


async def test_failed_retry_commit_keeps_no_partial_successor(
    authority, create_client, tmp_path, monkeypatch
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    association = await fresh_association(authority[0], authority[1])
    original = AsyncSession.commit

    async def failed(session):
        if await session.get(Workspace, "ws_successor") is not None:
            raise SQLAlchemyError("synthetic commit refusal")
        await original(session)

    monkeypatch.setattr(AsyncSession, "commit", failed)
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces/ws_cloud_stable/retry",
        json={"hosted": association},
        headers={**headers, "Idempotency-Key": "commit"},
    )
    assert response.status_code == 500
    async with authority[2]() as session:
        assert await session.get(Workspace, "ws_successor") is None


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
@pytest.mark.parametrize("uncertain", ["claim", "runtime", "no-proof"])
async def test_successor_refuses_unreleased_or_unknown_source(
    authority, create_client, create_application, tmp_path, action, uncertain
):
    from sqlalchemy import delete

    from awf.db.models import WorkspaceEvent

    await source_workspace(authority, create_client, tmp_path, status="failed")
    async with authority[2].begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        if uncertain == "claim":
            row.execution_claimed_by = "old-producer"
        elif uncertain == "runtime":
            row.compose_project_name = "retained-runtime"
        else:
            await session.execute(
                delete(WorkspaceEvent).where(
                    WorkspaceEvent.event_type == "workspace.pre_launch_failed"
                )
            )
    if action == "retry":
        body = {"hosted": await fresh_association(authority[0], authority[1])}
        path = "/v1/workspaces/ws_cloud_stable/retry"
    else:
        body = await adoption(create_application, authority, source="ws_cloud_stable")
        path = "/v1/workspaces/adopt-pr"
    response = await create_client[0].post(
        path, json=body, headers={**create_client[1], "Idempotency-Key": "uncertain"}
    )
    assert response.status_code == 409, response.text
    async with authority[2]() as session:
        assert await session.get(Workspace, "ws_successor") is None
