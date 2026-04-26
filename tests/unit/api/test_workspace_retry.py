"""Retry/requeue API contract tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


_V2_RETRY_BODY = {
    "repo": {
        "url": "git@github.com:example/retry-api.git",
        "base_branch": "development",
    },
    "task": {
        "title": "Retry API task",
        "prompt": "Retry this failed workspace.",
        "kind": "feature_branch_pr",
        "agent": "codex",
        "external_id": "TICKET-API-RETRY",
        "task_class": "test_task",
        "owned_paths": ["src/awf/api/retry.py"],
        "auto_merge": False,
    },
    "workspace": {"profile_ref": "python", "profile": None},
    "validation": {"commands": ["pytest tests/unit/api -q"], "requested_tier": 2},
    "resources": {},
}


async def _create_failed_workspace(client: AsyncClient, engine: AsyncEngine) -> str:
    created = await client.post("/v2/workspaces", json=_V2_RETRY_BODY)
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
    return workspace_id


@pytest.mark.unit
async def test_retry_endpoint_creates_new_requested_workspace(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    original_id = await _create_failed_workspace(client, engine)

    response = await client.post(f"/v1/workspaces/{original_id}/retry")

    assert response.status_code == 202
    body = response.json()
    assert body["source_workspace_id"] == original_id
    assert body["new_workspace_id"].startswith("ws_")
    assert body["new_workspace_id"] != original_id
    assert body["status"] == "requested"
    assert body["attempt_number"] == 2
    assert body["status_url"] == f"/v1/workspaces/{body['new_workspace_id']}"
    assert body["events_url"] == f"/v1/workspaces/{body['new_workspace_id']}/events"

    retried = await client.get(f"/v1/workspaces/{body['new_workspace_id']}")
    assert retried.status_code == 200
    retried_body = retried.json()
    assert retried_body["repo_url"] == _V2_RETRY_BODY["repo"]["url"]
    assert retried_body["branch_base"] == _V2_RETRY_BODY["repo"]["base_branch"]
    assert retried_body["task_title"] == _V2_RETRY_BODY["task"]["title"]
    assert retried_body["task_prompt"] == _V2_RETRY_BODY["task"]["prompt"]
    assert retried_body["task_external_id"] == _V2_RETRY_BODY["task"]["external_id"]
    assert retried_body["task_class"] == _V2_RETRY_BODY["task"]["task_class"]
    assert retried_body["owned_paths"] == _V2_RETRY_BODY["task"]["owned_paths"]
    assert retried_body["auto_merge"] is False
    assert retried_body["profile_ref"] == "python"
    assert retried_body["test_commands"] == _V2_RETRY_BODY["validation"]["commands"]
    assert retried_body["failure_reason"] is None
    assert retried_body["failure_message"] is None


@pytest.mark.unit
async def test_retry_endpoint_rejects_missing_workspace(client: AsyncClient) -> None:
    response = await client.post("/v1/workspaces/ws_missing_retry/retry")

    assert response.status_code == 404
    assert response.json()["error_code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.unit
async def test_retry_endpoint_rejects_non_terminal_workspace(client: AsyncClient) -> None:
    created = await client.post("/v2/workspaces", json=_V2_RETRY_BODY)
    assert created.status_code == 202
    workspace_id = str(created.json()["workspace_id"])

    response = await client.post(f"/v1/workspaces/{workspace_id}/retry")

    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "WORKSPACE_NOT_RETRYABLE"
    assert body["detail"]["status"] == "requested"
    assert body["detail"]["retryable_statuses"] == ["failed", "cancelled"]

