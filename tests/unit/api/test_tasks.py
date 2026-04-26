"""Task listing API tests for first-class task attempts."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


_V1_BODY = {
    "repo_url": "git@github.com:example/console.git",
    "branch_base": "main",
    "task_title": "Legacy console row",
    "task_prompt": "Expose useful workspace observability.",
    "task_external_id": "LEGACY-1",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


def _v2_body(
    *,
    external_id: str | None = "TICKET-123",
    title: str = "Add task attempts",
    owned_paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "repo": {
            "url": "git@github.com:example/console.git",
            "base_branch": "main",
        },
        "task": {
            "title": title,
            "prompt": "Persist first-class task attempts.",
            "kind": "feature_branch_pr",
            "agent": "codex",
            "external_id": external_id,
            "task_class": "test_task",
            "owned_paths": [] if owned_paths is None else owned_paths,
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {},
    }


async def _create_v1_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V1_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_v2_workspace(
    client: AsyncClient,
    *,
    external_id: str | None = "TICKET-123",
    title: str = "Add task attempts",
    headers: dict[str, str] | None = None,
) -> str:
    response = await client.post(
        "/v2/workspaces",
        json=_v2_body(external_id=external_id, title=title),
        headers=headers,
    )
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


class TestTaskList:
    @pytest.mark.unit
    async def test_lists_new_task_attempt_rows_and_legacy_workspace_rows(
        self,
        client: AsyncClient,
    ) -> None:
        legacy_workspace_id = await _create_v1_workspace(client)
        new_workspace_id = await _create_v2_workspace(
            client,
            external_id="TICKET-NEW",
            title="First-class task",
        )

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        items_by_workspace = {
            item["workspace_id"]: item for item in response.json()["items"]
        }
        legacy = items_by_workspace[legacy_workspace_id]
        new = items_by_workspace[new_workspace_id]

        assert legacy["task_id"] == "LEGACY-1"
        assert legacy["attempt_id"] is None
        assert legacy["attempt_number"] is None

        assert new["task_id"] == "TICKET-NEW"
        assert new["attempt_id"].startswith("att_")
        assert new["attempt_number"] == 1
        assert new["title"] == "First-class task"
        assert new["repo_url"] == "git@github.com:example/console.git"
        assert new["base_branch"] == "main"
        assert new["task_class"] == "test_task"
        assert new["status"] == WorkspaceStatus.requested.value

    @pytest.mark.unit
    async def test_idempotent_v2_replay_does_not_duplicate_attempts(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        from awf.db.models import Task, TaskAttempt

        headers = {"Idempotency-Key": "task-attempt-idem"}

        first_id = await _create_v2_workspace(
            client,
            external_id="TICKET-IDEM",
            title="Idempotent task",
            headers=headers,
        )
        second_id = await _create_v2_workspace(
            client,
            external_id="TICKET-IDEM",
            title="Idempotent task",
            headers=headers,
        )

        factory = make_session_factory(engine)
        async with factory() as session:
            tasks = list((await session.execute(select(Task))).scalars())
            attempts = list((await session.execute(select(TaskAttempt))).scalars())

        assert second_id == first_id
        assert len(tasks) == 1
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].workspace_id == first_id

    @pytest.mark.unit
    async def test_lists_latest_attempt_for_task(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        from awf.db.repositories import TaskAttemptRepository, TaskRepository

        factory = make_session_factory(engine)
        async with factory() as session:
            task = await TaskRepository(session).create_or_get(
                repo_url="git@github.com:example/console.git",
                base_branch="main",
                title="Manual retry",
                prompt="Retry this task.",
                external_id="TICKET-LATEST",
                idempotency_key=None,
                task_class="test_task",
                owned_paths=[],
            )
            first_workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/console.git",
                branch_base="main",
                task_title="First attempt",
                task_prompt="Retry this task.",
                task_external_id="TICKET-LATEST",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            second_workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/console.git",
                branch_base="main",
                task_title="Second attempt",
                task_prompt="Retry this task.",
                task_external_id="TICKET-LATEST",
                agent=AgentRuntime.codex.value,
                test_commands=[],
            )
            attempt_repo = TaskAttemptRepository(session)
            await attempt_repo.create_for_workspace(task=task, workspace=first_workspace)
            second_attempt = await attempt_repo.create_for_workspace(
                task=task,
                workspace=second_workspace,
            )
            second_workspace.status = WorkspaceStatus.ready.value
            second_attempt.status = WorkspaceStatus.ready.value
            await session.commit()

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        items = [
            item
            for item in response.json()["items"]
            if item["task_id"] == "TICKET-LATEST"
        ]
        assert len(items) == 1
        assert items[0]["attempt_id"] == second_attempt.id
        assert items[0]["attempt_number"] == 2
        assert items[0]["workspace_id"] == second_workspace.id
        assert items[0]["title"] == "Second attempt"
        assert items[0]["status"] == WorkspaceStatus.ready.value
