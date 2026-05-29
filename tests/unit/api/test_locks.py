"""Owned-path reservation API tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")


def _v2_body(
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "main",
    title: str = "Expose locks",
    task_class: str = "refactor_task",
    owned_paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "repo": {"url": repo_url, "base_branch": base_branch},
        "task": {
            "title": title,
            "prompt": "Expose owned-path reservations.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": task_class,
            "owned_paths": list(owned_paths or []),
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {},
    }


async def _create_lock_workspace(
    client: AsyncClient,
    *,
    repo_url: str = "git@github.com:example/app.git",
    title: str = "Expose locks",
    task_class: str = "refactor_task",
    owned_paths: list[str] | None = None,
) -> str:
    response = await client.post(
        "/v1/workspaces",
        json=_v2_body(
            repo_url=repo_url,
            title=title,
            task_class=task_class,
            owned_paths=owned_paths,
        ),
    )
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _set_workspace_status(
    engine: AsyncEngine,
    workspace_id: str,
    status: WorkspaceStatus,
    *,
    pr_url: str | None = None,
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.status = status.value
        workspace.pr_url = pr_url
        await session.commit()


@pytest.mark.unit
async def test_get_locks_lists_active_reservations_and_excludes_terminal_by_default(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    active_id = await _create_lock_workspace(
        client,
        title="API lock visibility",
        task_class="refactor_task",
        owned_paths=["src/awf/api/**", "tests/unit/api/**"],
    )
    terminal_id = await _create_lock_workspace(
        client,
        title="Completed reservation",
        task_class="docs_task",
        owned_paths=["docs/**"],
    )
    await _set_workspace_status(
        engine,
        active_id,
        WorkspaceStatus.running,
        pr_url="https://github.com/example/app/pull/12",
    )
    await _set_workspace_status(engine, terminal_id, WorkspaceStatus.completed)

    response = await client.get("/v1/locks")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["limit"] == 50
    assert body["cursor"] is None
    assert [item["workspace_id"] for item in body["items"]] == [active_id]
    item = body["items"][0]
    assert item["title"] == "API lock visibility"
    assert item["agent"] == "codex"
    assert item["status"] == "running"
    assert item["repo_url"] == "git@github.com:example/app.git"
    assert item["branch_base"] == "main"
    assert item["task_class"] == "refactor_task"
    assert item["owned_paths"] == ["src/awf/api/**", "tests/unit/api/**"]
    assert item["pr_url"] == "https://github.com/example/app/pull/12"
    assert "created_at" in item
    assert "updated_at" in item


@pytest.mark.unit
async def test_get_locks_applies_repo_task_class_status_and_limit_filters(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    matching_id = await _create_lock_workspace(
        client,
        repo_url="git@github.com:example/app.git",
        title="Matching reservation",
        task_class="test_task",
        owned_paths=["tests/unit/**"],
    )
    wrong_repo_id = await _create_lock_workspace(
        client,
        repo_url="git@github.com:example/docs.git",
        title="Wrong repo",
        task_class="test_task",
        owned_paths=["docs/**"],
    )
    wrong_class_id = await _create_lock_workspace(
        client,
        repo_url="git@github.com:example/app.git",
        title="Wrong class",
        task_class="docs_task",
        owned_paths=["README.md"],
    )
    await _set_workspace_status(engine, matching_id, WorkspaceStatus.ready)
    await _set_workspace_status(engine, wrong_repo_id, WorkspaceStatus.ready)
    await _set_workspace_status(engine, wrong_class_id, WorkspaceStatus.ready)

    response = await client.get(
        "/v1/locks",
        params={
            "repo_url": "git@github.com:example/app.git",
            "task_class": "test_task",
            "status": "ready",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    assert [item["workspace_id"] for item in response.json()["items"]] == [matching_id]


@pytest.mark.unit
async def test_get_locks_reports_has_more_and_accepts_next_cursor(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    first_id = await _create_lock_workspace(
        client,
        title="First reservation",
        owned_paths=["src/first/**"],
    )
    second_id = await _create_lock_workspace(
        client,
        title="Second reservation",
        owned_paths=["src/second/**"],
    )
    await _set_workspace_status(engine, first_id, WorkspaceStatus.ready)
    await _set_workspace_status(engine, second_id, WorkspaceStatus.ready)

    first_response = await client.get(
        "/v1/locks",
        params={"status": "ready", "limit": 1},
    )

    assert first_response.status_code == 200
    first_body = first_response.json()
    assert len(first_body["items"]) == 1
    assert first_body["has_more"] is True
    assert first_body["next_cursor"] is not None
    assert first_body["limit"] == 1
    assert first_body["cursor"] is None

    second_response = await client.get(
        "/v1/locks",
        params={"status": "ready", "limit": 1, "cursor": first_body["next_cursor"]},
    )

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert len(second_body["items"]) == 1
    assert second_body["has_more"] is False
    assert second_body["next_cursor"] is None
    assert second_body["limit"] == 1
    assert second_body["cursor"] == first_body["next_cursor"]
    returned_ids = {
        first_body["items"][0]["workspace_id"],
        second_body["items"][0]["workspace_id"],
    }
    assert returned_ids == {first_id, second_id}


@pytest.mark.unit
async def test_get_locks_invalid_cursor_returns_structured_400(client: AsyncClient) -> None:
    response = await client.get("/v1/locks", params={"cursor": "not-json"})

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_CURSOR",
        "message": "Invalid lock list cursor.",
    }


@pytest.mark.unit
async def test_get_locks_exposes_owned_path_overlap_risks(
    client: AsyncClient,
) -> None:
    existing_id = await _create_lock_workspace(
        client,
        title="Existing refactor",
        task_class="refactor_task",
        owned_paths=["src/awf/api/**"],
    )
    overlapping_response = await client.post(
        "/v1/workspaces",
        json=_v2_body(
            title="Overlapping docs",
            task_class="docs_task",
            owned_paths=["src/awf/api/routes/workspaces.py"],
        ),
    )

    assert overlapping_response.status_code == 202
    overlapping_id = str(overlapping_response.json()["workspace_id"])

    response = await client.get("/v1/locks")

    assert response.status_code == 200
    items = {item["workspace_id"]: item for item in response.json()["items"]}
    assert items[overlapping_id]["overlap_risks"] == [
        {
            "overlapping_workspace_id": existing_id,
            "overlapping_owned_path": "src/awf/api/**",
            "owned_path": "src/awf/api/routes/workspaces.py",
        }
    ]
    assert items[existing_id]["overlap_risks"] == [
        {
            "overlapping_workspace_id": overlapping_id,
            "overlapping_owned_path": "src/awf/api/routes/workspaces.py",
            "owned_path": "src/awf/api/**",
        }
    ]


@pytest.mark.unit
async def test_get_locks_ignores_internal_plan_artifact_only_overlap_risks(
    client: AsyncClient,
) -> None:
    existing_id = await _create_lock_workspace(
        client,
        title="Existing source work",
        owned_paths=["src/existing/**", "docs/awf-plans/**"],
    )
    response = await client.post(
        "/v1/workspaces",
        json=_v2_body(
            title="Independent source work",
            owned_paths=["src/requested/**", "docs/awf-plans/**"],
        ),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["warnings"] == []
    requested_id = str(body["workspace_id"])

    locks = await client.get("/v1/locks")

    assert locks.status_code == 200
    items = {item["workspace_id"]: item for item in locks.json()["items"]}
    assert items[existing_id]["owned_paths"] == ["src/existing/**", "docs/awf-plans/**"]
    assert items[requested_id]["owned_paths"] == ["src/requested/**", "docs/awf-plans/**"]
    assert items[existing_id]["overlap_risks"] == []
    assert items[requested_id]["overlap_risks"] == []
