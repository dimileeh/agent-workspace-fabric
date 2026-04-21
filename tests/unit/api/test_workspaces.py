"""Workspace API contract tests.

Each test runs against a fresh in-memory SQLite via the ``client`` fixture.
These are *unit*-flavoured tests because they don't spin up Docker or Postgres;
true integration + E2E tests live under tests/integration/ and tests/e2e/.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

_MINIMAL_BODY = {
    "repo_url": "git@github.com:dimileeh/aira-agent.git",
    "branch_base": "development",
    "task_title": "Add module docstring",
    "task_prompt": "Add a one-line docstring to src/aira_agent/api/main.py.",
    "agent": "openclaw",
    "test_commands": ["pytest -q"],
}


class TestCreateWorkspace:
    @pytest.mark.unit
    async def test_returns_202_with_workspace_id(self, client: AsyncClient) -> None:
        response = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        assert response.status_code == 202

        body = response.json()
        assert body["workspace_id"].startswith("ws_")
        assert body["status"] == "requested"
        assert body["version"] == 1
        assert body["status_url"] == f"/v1/workspaces/{body['workspace_id']}"
        assert "accepted_at" in body

    @pytest.mark.unit
    async def test_rejects_empty_task_prompt(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "task_prompt": ""}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_rejects_unknown_agent(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "agent": "my-shiny-agent"}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422

    @pytest.mark.unit
    async def test_rejects_extra_fields(self, client: AsyncClient) -> None:
        bad = {**_MINIMAL_BODY, "hax0r_field": "value"}
        response = await client.post("/v1/workspaces", json=bad)
        assert response.status_code == 422


class TestIdempotency:
    @pytest.mark.unit
    async def test_same_key_same_body_returns_same_workspace(self, client: AsyncClient) -> None:
        headers = {"Idempotency-Key": "abc-123"}
        r1 = await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)
        r2 = await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["workspace_id"] == r2.json()["workspace_id"]

    @pytest.mark.unit
    async def test_same_key_different_body_returns_409(self, client: AsyncClient) -> None:
        headers = {"Idempotency-Key": "abc-123"}
        await client.post("/v1/workspaces", json=_MINIMAL_BODY, headers=headers)

        mutated = {**_MINIMAL_BODY, "task_title": "different"}
        r2 = await client.post("/v1/workspaces", json=mutated, headers=headers)

        assert r2.status_code == 409
        assert r2.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


class TestGetWorkspace:
    @pytest.mark.unit
    async def test_returns_workspace_shape(self, client: AsyncClient) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]

        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200

        body = response.json()
        assert body["id"] == ws_id
        assert body["status"] == "requested"
        assert body["version"] == 1
        assert body["task_title"] == _MINIMAL_BODY["task_title"]
        assert body["agent"] == "openclaw"
        assert body["test_commands"] == _MINIMAL_BODY["test_commands"]

    @pytest.mark.unit
    async def test_returns_404_for_unknown_id(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces/ws_doesnotexist000000000000")
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"


class TestListWorkspaces:
    @pytest.mark.unit
    async def test_returns_empty_list_when_none(self, client: AsyncClient) -> None:
        response = await client.get("/v1/workspaces")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.unit
    async def test_returns_created_workspaces_newest_first(self, client: AsyncClient) -> None:
        ids: list[str] = []
        for title in ["first", "second", "third"]:
            body = {**_MINIMAL_BODY, "task_title": title}
            create = await client.post("/v1/workspaces", json=body)
            ids.append(create.json()["workspace_id"])

        response = await client.get("/v1/workspaces")
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 3
        # Newest first.
        assert [r["id"] for r in results] == list(reversed(ids))
