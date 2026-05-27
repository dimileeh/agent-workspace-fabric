"""Task listing API tests for first-class task attempts."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.tasks as tasks_route
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import TaskAttemptRepository, WorkspaceRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")


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
    agent: str = "codex",
    model: str | None = None,
) -> dict[str, object]:
    task = {
        "title": title,
        "prompt": "Persist first-class task attempts.",
        "kind": "feature_branch_pr",
        "agent": agent,
        "external_id": external_id,
        "task_class": "test_task",
        "owned_paths": [] if owned_paths is None else owned_paths,
    }
    if model is not None:
        task["model"] = model
    return {
        "repo": {
            "url": "git@github.com:example/console.git",
            "base_branch": "main",
        },
        "task": task,
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {},
    }


async def _create_v1_workspace(client: AsyncClient) -> str:
    response = await client.post("/v1/workspaces", json=_V1_BODY)
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _create_workspace(
    client: AsyncClient,
    *,
    external_id: str | None = "TICKET-123",
    title: str = "Add task attempts",
    agent: str = "codex",
    model: str | None = None,
    provider_readiness_override: bool = False,
    headers: dict[str, str] | None = None,
) -> str:
    payload = _v2_body(external_id=external_id, title=title, agent=agent, model=model)
    if provider_readiness_override:
        payload["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "test fixture only observes task identity",
        }
    response = await client.post(
        "/v1/workspaces",
        json=payload,
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
        new_workspace_id = await _create_workspace(
            client,
            external_id="TICKET-NEW",
            title="First-class task",
        )

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 50
        assert body["cursor"] is None
        items_by_workspace = {item["workspace_id"]: item for item in body["items"]}
        legacy = items_by_workspace[legacy_workspace_id]
        new = items_by_workspace[new_workspace_id]

        assert legacy["task_id"] == "LEGACY-1"
        assert legacy["attempt_id"].startswith("att_")
        assert legacy["attempt_number"] == 1

        assert new["task_id"] == "TICKET-NEW"
        assert new["attempt_id"].startswith("att_")
        assert new["attempt_number"] == 1
        assert new["title"] == "First-class task"
        assert new["repo_url"] == "git@github.com:example/console.git"
        assert new["base_branch"] == "main"
        assert new["task_class"] == "test_task"
        assert new["status"] == WorkspaceStatus.requested.value

    @pytest.mark.unit
    async def test_task_rows_expose_effective_identity_and_usage_summary(
        self,
        client: AsyncClient,
    ) -> None:
        legacy_workspace_id = await _create_v1_workspace(client)
        attempt_workspace_id = await _create_workspace(
            client,
            external_id="TICKET-OBSERVE",
            title="Observe model identity",
            agent="opencode",
            provider_readiness_override=True,
        )

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        items_by_workspace = {item["workspace_id"]: item for item in response.json()["items"]}
        legacy = items_by_workspace[legacy_workspace_id]
        attempt = items_by_workspace[attempt_workspace_id]
        assert legacy["agent_model"] == "gpt-5.5"
        assert legacy["agent_effort"] == "xhigh"
        assert legacy["agent_model_source"] == "default"
        assert legacy["agent_effort_source"] == "default"
        assert attempt["agent_model"] == "ollama/kimi-k2.6:cloud"
        assert attempt["agent_effort"] == "xhigh"
        assert attempt["agent_model_source"] == "default"
        assert attempt["agent_effort_source"] == "default"
        for row in (legacy, attempt):
            assert row["llm_usage"] == {
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_output_tokens": None,
                "total_tokens": None,
                "cost_estimate": None,
                "currency": None,
                "status": "unavailable",
                "source": "none",
                "reason": "usage_not_reported",
            }
            assert row.get("pricing") is None

    @pytest.mark.unit
    async def test_list_tasks_route_merges_attempt_and_legacy_rows_directly(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        legacy_workspace_id = await _create_v1_workspace(client)
        new_workspace_id = await _create_workspace(
            client,
            external_id="TICKET-DIRECT",
            title="Direct route task",
        )

        factory = make_session_factory(engine)
        async with factory() as session:
            response = await tasks_route.list_tasks(session=session)

        items_by_workspace = {item.workspace_id: item for item in response.items}
        assert set(items_by_workspace) == {legacy_workspace_id, new_workspace_id}
        assert items_by_workspace[legacy_workspace_id].attempt_id is not None
        assert items_by_workspace[legacy_workspace_id].attempt_number == 1
        assert items_by_workspace[new_workspace_id].attempt_id is not None
        assert items_by_workspace[new_workspace_id].task_id == "TICKET-DIRECT"

    @pytest.mark.unit
    async def test_idempotent_v2_replay_does_not_duplicate_attempts(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        from awf.db.models import Task, TaskAttempt

        headers = {"Idempotency-Key": "task-attempt-idem"}

        first_id = await _create_workspace(
            client,
            external_id="TICKET-IDEM",
            title="Idempotent task",
            headers=headers,
        )
        second_id = await _create_workspace(
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
        items = [item for item in response.json()["items"] if item["task_id"] == "TICKET-LATEST"]
        assert len(items) == 1
        assert items[0]["attempt_id"] == second_attempt.id
        assert items[0]["attempt_number"] == 2
        assert items[0]["workspace_id"] == second_workspace.id
        assert items[0]["title"] == "Second attempt"
        assert items[0]["status"] == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_tasks_exposes_canonical_attempt_and_candidate_fields(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(
            client,
            external_id="TICKET-CANONICAL",
            title="Canonical task",
        )

        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            for target in (
                WorkspaceStatus.provisioning,
                WorkspaceStatus.ready,
                WorkspaceStatus.running,
                WorkspaceStatus.validating,
                WorkspaceStatus.pushing,
            ):
                await repo.transition(workspace, to=target, reason_code="TEST")
            workspace.branch_name = "awf/canonical-task"
            workspace.remote_push_branch = "awf/canonical-task"
            workspace.base_commit = "a" * 40
            workspace.pr_url = "https://github.com/example/console/pull/41"
            workspace.pr_number = 41
            await repo.transition(
                workspace,
                to=WorkspaceStatus.monitoring_pr,
                reason_code="PR_OPENED",
            )
            await session.commit()

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        item = next(
            item for item in response.json()["items"] if item["task_id"] == "TICKET-CANONICAL"
        )
        assert item["is_canonical_for_merge"] is True
        assert item["canonical_attempt_id"] == item["attempt_id"]
        assert item["candidate_id"].startswith("mc_")
        assert item["candidate_status"] == "open"
        assert item["readiness"] == {
            "ready": True,
            "manual_merge_required": False,
            "waiting_for_monitor": False,
            "failed_or_cancelled": False,
            "completed": False,
            "not_canonical": False,
            "stale": False,
            "stale_reason": None,
        }

    @pytest.mark.unit
    async def test_tasks_batches_canonical_attempt_lookup(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_ids = [
            await _create_workspace(
                client,
                external_id=f"TICKET-CANONICAL-BATCH-{number}",
                title=f"Canonical batch task {number}",
            )
            for number in range(2)
        ]

        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            for number, workspace_id in enumerate(workspace_ids, start=1):
                workspace = await repo.get(workspace_id)
                assert workspace is not None
                for target in (
                    WorkspaceStatus.provisioning,
                    WorkspaceStatus.ready,
                    WorkspaceStatus.running,
                    WorkspaceStatus.validating,
                    WorkspaceStatus.pushing,
                ):
                    await repo.transition(workspace, to=target, reason_code="TEST")
                workspace.branch_name = f"awf/canonical-batch-{number}"
                workspace.remote_push_branch = f"awf/canonical-batch-{number}"
                workspace.base_commit = "a" * 40
                workspace.pr_url = f"https://github.com/example/console/pull/{number}"
                workspace.pr_number = number
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.monitoring_pr,
                    reason_code="PR_OPENED",
                )
            await session.commit()

        async def fail_get_canonical_for_task(
            self: TaskAttemptRepository,
            task_id: str,
        ) -> object:
            raise AssertionError(f"list_tasks should batch canonical lookup for task {task_id}")

        monkeypatch.setattr(
            TaskAttemptRepository,
            "get_canonical_for_task",
            fail_get_canonical_for_task,
        )

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        items = [
            item
            for item in response.json()["items"]
            if item["task_id"].startswith("TICKET-CANONICAL-BATCH-")
        ]
        assert len(items) == 2
        assert all(item["canonical_attempt_id"] == item["attempt_id"] for item in items)

    @pytest.mark.unit
    async def test_task_attempts_endpoint_resolves_external_id_and_lists_lineage_newest_first(
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
                title="Lineage task",
                prompt="Retry this task.",
                external_id="TICKET-LINEAGE",
                idempotency_key=None,
                task_class="test_task",
                owned_paths=[],
            )
            workspaces = [
                await WorkspaceRepository(session).create(
                    repo_url="git@github.com:example/console.git",
                    branch_base="main",
                    task_title=f"Attempt {number}",
                    task_prompt="Retry this task.",
                    task_external_id="TICKET-LINEAGE",
                    agent=AgentRuntime.codex.value,
                    test_commands=[],
                )
                for number in (1, 2, 3)
            ]
            attempt_repo = TaskAttemptRepository(session)
            first = await attempt_repo.create_for_workspace(task=task, workspace=workspaces[0])
            second = await attempt_repo.create_for_workspace(
                task=task,
                workspace=workspaces[1],
                parent_attempt_id=first.id,
                redispatch_from_attempt_id=first.id,
            )
            third = await attempt_repo.create_for_workspace(
                task=task,
                workspace=workspaces[2],
                parent_attempt_id=second.id,
                redispatch_from_attempt_id=second.id,
            )
            await session.commit()

        response = await client.get("/v1/tasks/TICKET-LINEAGE/attempts")

        assert response.status_code == 200
        body = response.json()
        assert body["task_id"] == task.id
        assert body["task_ref"] == "TICKET-LINEAGE"
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["limit"] == 100
        assert body["cursor"] is None
        assert [item["attempt_id"] for item in body["items"]] == [
            third.id,
            second.id,
            first.id,
        ]
        assert [item["attempt_number"] for item in body["items"]] == [3, 2, 1]
        assert body["items"][0]["parent_attempt_id"] == second.id
        assert body["items"][0]["redispatch_from_attempt_id"] == second.id
        assert body["items"][1]["parent_attempt_id"] == first.id
        assert body["items"][2]["parent_attempt_id"] is None

    @pytest.mark.unit
    async def test_task_attempts_missing_task_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/v1/tasks/NO-SUCH-TASK/attempts")

        assert response.status_code == 404
        assert response.json()["detail"] == {
            "error_code": "NOT_FOUND",
            "message": "No task with ref NO-SUCH-TASK",
        }

    @pytest.mark.unit
    async def test_task_attempts_validates_limit_bounds(self, client: AsyncClient) -> None:
        response = await client.get("/v1/tasks/NO-SUCH-TASK/attempts", params={"limit": 0})

        assert response.status_code == 422

    @pytest.mark.unit
    async def test_task_attempts_route_function_raises_404_for_missing_task(
        self,
        engine: AsyncEngine,
    ) -> None:
        async with make_session_factory(engine)() as session:
            with pytest.raises(HTTPException) as exc_info:
                await tasks_route.list_task_attempts(
                    "NO-SUCH-TASK",
                    session=session,
                )

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == {
            "error_code": "NOT_FOUND",
            "message": "No task with ref NO-SUCH-TASK",
        }

    @pytest.mark.unit
    async def test_task_attempts_route_function_returns_empty_attempt_list(
        self,
        engine: AsyncEngine,
    ) -> None:
        from awf.db.repositories import TaskRepository

        factory = make_session_factory(engine)
        async with factory() as session:
            task = await TaskRepository(session).create_or_get(
                repo_url="git@github.com:example/console.git",
                base_branch="main",
                title="No attempts",
                prompt="Do not create attempts.",
                external_id="TICKET-NO-ATTEMPTS",
                idempotency_key=None,
                task_class="test_task",
                owned_paths=[],
            )
            await session.commit()

        async with factory() as session:
            response = await tasks_route.list_task_attempts(
                "TICKET-NO-ATTEMPTS",
                session=session,
            )

        assert response.task_id == task.id
        assert response.task_ref == "TICKET-NO-ATTEMPTS"
        assert response.items == []
        assert response.next_cursor is None
        assert response.has_more is False
        assert response.limit == 100
        assert response.cursor is None
