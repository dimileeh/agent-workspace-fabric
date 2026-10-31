"""Strict caller attestations and durable source authority on the actual API."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from awf.db.models import Task, TaskAttempt, Workspace
from tests.integration.test_hosted_lifecycle_parity import source_workspace
from tests.integration.test_hosted_lifecycle_successors import adoption
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("change", ["missing", "malformed", "workspace", "owner", "identity"])
async def test_control_requires_exact_stored_source(authority, create_client, tmp_path, change):
    await source_workspace(authority, create_client, tmp_path)
    async with authority[2].begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        policy = deepcopy(row.task_policy)
        if change == "missing":
            policy.pop("hosted_node")
        elif change == "malformed":
            policy["hosted_node"] = {"workspace_id": row.id}
        elif change == "workspace":
            policy["hosted_node"]["workspace_id"] = "ws_another"
            for binding in policy["hosted_node"]["credentials"].values():
                binding["workspace_id"] = "ws_another"
        elif change == "identity":
            for binding in policy["hosted_node"]["credentials"].values():
                binding["identity"]["boot_epoch"] += 1
        else:
            row.node_id = "another-owner"
        row.task_policy = policy
    response = await create_client[0].post(
        "/v1/workspaces/ws_cloud_stable/guide",
        json={
            "directive": "Continue review",
            "hosted_authorization": {"namespace": "tenant", "actor": "caller"},
        },
        headers={**create_client[1], "Idempotency-Key": "stored-source"},
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "authorization",
    [
        {"namespace": "tenant", "actor": " "},
        {"namespace": "tenant", "actor": "a" * 257},
        {"namespace": "tenant", "actor": "caller", "allow_grants": "true"},
        {"namespace": "tenant", "actor": "caller", "allow_grants": 1},
        {"namespace": "tenant", "actor": "caller", "allow_grants": None},
        {"namespace": "tenant", "actor": "caller", "admin": True},
    ],
)
async def test_attestation_is_strict_and_bounded(authority, create_client, tmp_path, authorization):
    await source_workspace(authority, create_client, tmp_path)
    response = await create_client[0].post(
        "/v1/workspaces/ws_cloud_stable/guide",
        json={"directive": "Continue", "hosted_authorization": authorization},
        headers={**create_client[1], "Idempotency-Key": "strict"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("change", ["missing", "repo", "pr", "execution"])
async def test_scoped_adoption_validates_source_and_execution(
    authority, create_client, create_application, tmp_path, change
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    body = await adoption(create_application, authority, source="ws_cloud_stable")
    if change == "missing":
        body["source_workspace_id"] = "ws_missing"
    elif change == "execution":
        body["execution"] = {"mode": "hosted"}
    else:
        async with authority[2].begin() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            if change == "repo":
                row.repo_url = "https://github.com/foreign/repo.git"
            else:
                row.pr_number = 99
    response = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert response.status_code == (422 if change == "execution" else 403), response.text
    async with authority[2]() as session:
        assert await session.get(Workspace, "ws_successor") is None


@pytest.mark.parametrize("namespace", ["tenant", "foreign", "attention", "owner", "legacy"])
async def test_adoption_task_join_preserves_tenant(
    authority, create_client, create_application, tmp_path, namespace
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    body = await adoption(create_application, authority)
    async with authority[2].begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        task = await session.scalar(
            select(Task).join(TaskAttempt).where(TaskAttempt.workspace_id == row.id)
        )
        task.external_id = row.task_external_id = "cloud-task-scoped"
        source_task_id = task.id
        body.pop("pr_url")
        body.update(
            repo_url=row.repo_url,
            pr_number=42,
            external_id=row.task_external_id,
            task_title=row.task_title,
            owned_paths=row.owned_paths,
            task_class=row.task_class,
        )
        policy = deepcopy(row.task_policy)
        for binding in policy["hosted_node"]["credentials"].values():
            if namespace == "foreign":
                binding["namespace"] = namespace
            if namespace == "owner":
                binding["identity"]["owner"] = "other-owner"
        if namespace == "attention":
            policy["hosted_recovery"] = {"disposition": "attention"}
        if namespace == "legacy":
            policy.pop("hosted_node")
        row.task_policy = policy
    response = await create_client[0].post(
        "/v1/workspaces/adopt-pr", json=body, headers=create_client[1]
    )
    assert response.status_code == (202 if namespace == "tenant" else 403), response.text
    if namespace == "tenant":
        assert response.json()["task_id"] == source_task_id
