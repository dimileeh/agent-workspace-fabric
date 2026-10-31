"""Real hosted HTTP, PostgreSQL guards and ordinary lifecycle services."""

import base64
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.common.github_client import PullRequestAdoptionMetadata
from awf.db.models import Operation, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.schemas import Install, Preparation
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


async def fresh_association(runtime, value, workspace_id="ws_successor", namespace="tenant"):
    association = deepcopy(value["hosted"])
    association["workspace_id"] = workspace_id
    for role, binding in association["credentials"].items():
        binding.update(workspace_id=workspace_id, namespace=namespace)
        for field in ("preparation_id", "grant_id", "idempotency_key"):
            binding[field] = f"{namespace}-{workspace_id}-{role}-{field}"
        await runtime.receiver.prepare(
            Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
        )
        installed = await runtime.receiver.install(
            Install.model_validate(
                {**binding, "material": base64.b64encode(f"fresh-{role}".encode()).decode()}
            )
        )
        assert installed.status == "current"
    return HostedWorkspace.model_validate(association).model_dump(mode="json")


async def source_workspace(authority, create_client, tmp_path, status="monitoring_pr"):
    _, value, factory = authority
    client, headers = create_client
    created = await client.post(
        "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "source"}
    )
    assert created.status_code == 202, created.text
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = status
        row.node_id = authority[0].receiver.identity.owner
        if status == "failed":
            await WorkspaceRepository(session).add_event(
                row,
                event_type="workspace.pre_launch_failed",
                reason_code="SYNTHETIC_PRELAUNCH_FAILURE",
            )
        if status == "monitoring_pr":
            row.pr_url = "https://github.com/example/repo/pull/42"
            row.pr_number = 42
            row.branch_name = row.remote_push_branch = "feature/existing"
            row.compose_project_name = "awf_ws_cloud_stable"
            row.compose_file_path = str(compose)


@pytest.mark.parametrize("action", ["guide", "remonitor"])
async def test_hosted_existing_control_uses_current_authority_and_replays(
    authority, create_client, tmp_path, action
):
    await source_workspace(authority, create_client, tmp_path)
    client, headers = create_client
    body = {
        "reason": "caller decision",
        "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
    }
    if action == "guide":
        body["directive"] = "Continue the existing review"
    headers = {**headers, "Idempotency-Key": f"control-{action}"}
    first = await client.post(
        f"/v1/workspaces/ws_cloud_stable/{action}", json=body, headers=headers
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/v1/workspaces/ws_cloud_stable/{action}", json=body, headers=headers
    )
    assert replay.status_code == 200, replay.text
    assert first.json()["operation_id"] == replay.json()["operation_id"]
    async with authority[2]() as session:
        rows = list(await session.scalars(select(Operation).where(Operation.type == action)))
        assert len(rows) == 1


async def test_hosted_retry_new_association_and_lost_response_replay(
    authority, create_client, tmp_path
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    association = await fresh_association(runtime, value)
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "retry-once"}
    body = {"hosted": association}
    first = await client.post("/v1/workspaces/ws_cloud_stable/retry", json=body, headers=headers)
    assert first.status_code == 202, first.text
    replay = await client.post("/v1/workspaces/ws_cloud_stable/retry", json=body, headers=headers)
    assert replay.status_code == 202, replay.text
    assert first.json() == replay.json()
    assert first.json()["new_workspace_id"] == "ws_successor"
    async with factory() as session:
        source = await session.get(Workspace, "ws_cloud_stable")
        target = await session.get(Workspace, "ws_successor")
        assert target.task_policy["hosted_node"] == association
        assert (
            target.repo_url == source.repo_url
            and target.task_external_id == source.task_external_id
        )
        assert target.task_policy["hosted_node"] != source.task_policy["hosted_node"]
    assert (await runtime.workspace("ws_successor")).environment["GH_TOKEN"] == "fresh-github"


async def test_hosted_adopt_new_association_and_attach_exact_only(
    authority, create_client, create_application
):
    runtime, value, factory = authority
    association = await fresh_association(runtime, value)
    app, _ = create_application
    fetcher = AsyncMock(
        return_value=PullRequestAdoptionMetadata(
            number=42,
            head_ref="feature/existing",
            head_repo_slug="example/repo",
            base_ref="main",
            head_sha="a" * 40,
            base_sha="b" * 40,
            state="OPEN",
            is_draft=False,
            closed=False,
            merged=False,
            author="caller",
            url="https://github.com/example/repo/pull/42",
            title="Existing PR",
        )
    )
    app.state.pr_adoption_metadata_fetcher = fetcher
    client, headers = create_client
    body = {
        "pr_url": "https://github.com/example/repo/pull/42",
        "execution": {"mode": "local"},
        "hosted": association,
    }
    first = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    assert first.status_code == 202, first.text
    replay = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    assert replay.status_code == 202, replay.text
    assert first.json()["workspace_id"] == replay.json()["workspace_id"] == "ws_successor"
    assert replay.json()["attached_existing"] is True
    assert fetcher.await_count == 1
    async with factory() as session:
        row = await session.get(Workspace, "ws_successor")
        assert row.task_policy["hosted_node"] == association
