"""Lock reservation API tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


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
            "prompt": "Expose lock reservations.",
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
        "/v2/workspaces",
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
