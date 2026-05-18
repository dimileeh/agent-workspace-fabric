"""Advisory overlap graph API tests."""

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
    title: str = "Overlap graph task",
    task_class: str = "refactor_task",
    owned_paths: list[str] | None = None,
) -> dict[str, object]:
    return {
        "repo": {"url": repo_url, "base_branch": base_branch},
        "task": {
            "title": title,
            "prompt": "Expose advisory overlap graph.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": task_class,
            "owned_paths": list(owned_paths or []),
        },
        "workspace": {"profile_ref": "auto", "profile": None},
        "validation": {"commands": ["pytest -q"], "requested_tier": 1},
        "resources": {},
    }


async def _create_graph_workspace(
    client: AsyncClient,
    *,
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "main",
    title: str = "Overlap graph task",
    task_class: str = "refactor_task",
    owned_paths: list[str] | None = None,
) -> str:
    response = await client.post(
        "/v1/workspaces",
        json=_v2_body(
            repo_url=repo_url,
            base_branch=base_branch,
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
) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = status.value
        await session.commit()


@pytest.mark.unit
async def test_get_overlap_graph_serializes_reason_codes_and_affected_workspace_ids(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    running_id = await _create_graph_workspace(
        client,
        title="Running service change",
        owned_paths=["src/awf/service/**"],
    )
    queued_id = await _create_graph_workspace(
        client,
        title="Queued service file",
        owned_paths=["src/awf/service/workspaces.py"],
    )
    await _create_graph_workspace(
        client,
        title="Wrong class",
        task_class="docs_task",
        owned_paths=["src/awf/service/workspaces.py"],
    )
    await _set_workspace_status(engine, running_id, WorkspaceStatus.running)

    response = await client.get(
        "/v1/locks/overlap-graph",
        params={
            "repo_url": "git@github.com:example/app.git",
            "base_branch": "main",
            "task_class": "refactor_task",
            "queue_state": "all",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "has_more" not in body
    assert body["summary"] == {
        "node_count": 2,
        "queued_count": 1,
        "running_count": 1,
        "edge_count": 1,
        "affected_workspace_count": 2,
        "has_more": False,
    }
    assert {node["workspace_id"] for node in body["nodes"]} == {running_id, queued_id}
    assert {node["queue_state"] for node in body["nodes"]} == {"queued", "running"}
    edge = body["edges"][0]
    assert edge["reason_code"] == "OWNED_PATH_OVERLAP_RISK"
    assert edge["severity"] == "advisory"
    assert edge["blocks_launch"] is False
    assert edge["affected_workspace_ids"] == sorted([running_id, queued_id])
    match = edge["path_matches"][0]
    assert {match["left_workspace_id"], match["right_workspace_id"]} == {
        running_id,
        queued_id,
    }
    assert {match["left_owned_path"], match["right_owned_path"]} == {
        "src/awf/service/**",
        "src/awf/service/workspaces.py",
    }
    assert match["match_reason_code"] == "OWNED_PATH_WILDCARD_MATCH"
    assert match["explanation"] in {
        (
            "Wildcard owned-path prefixes overlap: "
            "src/awf/service/** <-> src/awf/service/workspaces.py."
        ),
        (
            "Wildcard owned-path prefixes overlap: "
            "src/awf/service/workspaces.py <-> src/awf/service/**."
        ),
    }


@pytest.mark.unit
async def test_get_overlap_graph_applies_queue_state_filter_and_validates_enum(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    queued_left_id = await _create_graph_workspace(
        client,
        repo_url="git@github.com:example/queued.git",
        title="Queued left",
        owned_paths=["queued/**"],
    )
    queued_right_id = await _create_graph_workspace(
        client,
        repo_url="git@github.com:example/queued.git",
        title="Queued right",
        owned_paths=["queued/file.py"],
    )
    running_id = await _create_graph_workspace(
        client,
        repo_url="git@github.com:example/queued.git",
        title="Running overlap",
        owned_paths=["queued/file.py"],
    )
    await _set_workspace_status(engine, queued_right_id, WorkspaceStatus.ready)
    await _set_workspace_status(engine, running_id, WorkspaceStatus.running)

    queued_response = await client.get(
        "/v1/locks/overlap-graph",
        params={
            "repo_url": "git@github.com:example/queued.git",
            "queue_state": "queued",
        },
    )
    invalid_response = await client.get(
        "/v1/locks/overlap-graph",
        params={"queue_state": "blocked"},
    )

    assert queued_response.status_code == 200
    queued_body = queued_response.json()
    assert {node["workspace_id"] for node in queued_body["nodes"]} == {
        queued_left_id,
        queued_right_id,
    }
    assert queued_body["edges"][0]["affected_workspace_ids"] == sorted(
        [queued_left_id, queued_right_id]
    )
    assert invalid_response.status_code == 422


@pytest.mark.unit
async def test_get_overlap_graph_serializes_bounded_path_match_metadata(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    from awf.service.overlap_graph import OVERLAP_GRAPH_PATH_MATCH_LIMIT

    path_count = OVERLAP_GRAPH_PATH_MATCH_LIMIT + 2
    running_id = await _create_graph_workspace(
        client,
        title="Broad running",
        owned_paths=[f"src/pkg{index}/**" for index in range(path_count)],
    )
    queued_id = await _create_graph_workspace(
        client,
        title="Broad queued",
        owned_paths=[f"src/pkg{index}/feature.py" for index in range(path_count)],
    )
    await _set_workspace_status(engine, running_id, WorkspaceStatus.running)

    response = await client.get(
        "/v1/locks/overlap-graph",
        params={"repo_url": "git@github.com:example/app.git"},
    )

    assert response.status_code == 200
    edge = response.json()["edges"][0]
    assert edge["affected_workspace_ids"] == sorted([running_id, queued_id])
    assert edge["path_match_count"] == path_count
    assert edge["path_matches_truncated"] is True
    assert len(edge["path_matches"]) == OVERLAP_GRAPH_PATH_MATCH_LIMIT
