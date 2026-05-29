"""Advisory overlap graph service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.overlap_graph as overlap_graph
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.mark.unit
async def test_overlap_graph_edge_builder_returns_empty_for_single_workspace() -> None:
    assert await overlap_graph._workspace_overlap_edges(()) == ()


@pytest.mark.unit
def test_overlap_graph_rejects_status_outside_queue_states() -> None:
    with pytest.raises(ValueError, match="not part of the overlap graph"):
        overlap_graph._queue_state_for_status(WorkspaceStatus.completed.value)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    title: str,
    repo_url: str = "git@github.com:example/app.git",
    branch_base: str = "main",
    task_class: str | None = "refactor_task",
    owned_paths: list[str] | None = None,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    created_at: datetime,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=title,
            task_prompt="Inspect advisory owned-path overlap.",
            task_class=task_class,
            owned_paths=list(owned_paths or []),
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.created_at = created_at
        workspace.updated_at = created_at + timedelta(minutes=5)
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_overlap_graph_builds_advisory_edges_for_same_repo_base(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    running_id = await _workspace(
        session_factory,
        title="Running refactor",
        owned_paths=["src/awf/service/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    queued_id = await _workspace(
        session_factory,
        title="Queued API work",
        owned_paths=["src/awf/service/workspaces.py"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert [node.workspace_id for node in graph.nodes] == [queued_id, running_id]
    assert graph.summary.node_count == 2
    assert graph.summary.edge_count == 1
    edge = graph.edges[0]
    assert edge.reason_code == "OWNED_PATH_OVERLAP_RISK"
    assert edge.severity == "advisory"
    assert edge.blocks_launch is False
    assert edge.affected_workspace_ids == tuple(sorted([running_id, queued_id]))
    assert {edge.left_workspace_id, edge.right_workspace_id} == {running_id, queued_id}


@pytest.mark.unit
async def test_overlap_graph_ignores_internal_plan_artifact_only_edges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    running_id = await _workspace(
        session_factory,
        title="Running source A",
        owned_paths=["src/feature-a/**", "docs/awf-plans/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    queued_id = await _workspace(
        session_factory,
        title="Queued source B",
        owned_paths=["src/feature-b/**", "docs/awf-plans/**"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert {node.workspace_id for node in graph.nodes} == {running_id, queued_id}
    node_paths = {node.workspace_id: node.owned_paths for node in graph.nodes}
    assert node_paths[running_id] == ("src/feature-a/**", "docs/awf-plans/**")
    assert node_paths[queued_id] == ("src/feature-b/**", "docs/awf-plans/**")
    assert graph.summary.edge_count == 0
    assert graph.edges == ()


@pytest.mark.unit
async def test_overlap_graph_ignores_plan_artifact_matches_but_keeps_real_edge(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    running_id = await _workspace(
        session_factory,
        title="Running shared source",
        owned_paths=["src/shared/**", "docs/awf-plans/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    queued_id = await _workspace(
        session_factory,
        title="Queued shared source",
        owned_paths=["src/shared/module.py", "docs/awf-plans/**"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.affected_workspace_ids == tuple(sorted([running_id, queued_id]))
    assert edge.path_match_count == 1
    assert {
        frozenset([match.left_owned_path, match.right_owned_path]) for match in edge.path_matches
    } == {frozenset(["src/shared/**", "src/shared/module.py"])}


@pytest.mark.unit
async def test_overlap_graph_filters_to_running_and_queued_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    active_left_id = await _workspace(
        session_factory,
        title="Active left",
        owned_paths=["src/**"],
        status=WorkspaceStatus.ready,
        created_at=now,
    )
    active_right_id = await _workspace(
        session_factory,
        title="Active right",
        owned_paths=["src/awf/api.py"],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=1),
    )
    excluded_ids = {
        await _workspace(
            session_factory,
            title=f"Excluded {status.value}",
            owned_paths=["src/awf/api.py"],
            status=status,
            created_at=now + timedelta(minutes=2 + index),
        )
        for index, status in enumerate(
            [
                WorkspaceStatus.completed,
                WorkspaceStatus.failed,
                WorkspaceStatus.cancelled,
                WorkspaceStatus.destroying,
                WorkspaceStatus.destroyed,
            ]
        )
    }
    wrong_repo_id = await _workspace(
        session_factory,
        title="Wrong repo",
        repo_url="git@github.com:example/other.git",
        owned_paths=["src/awf/api.py"],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=8),
    )
    wrong_base_id = await _workspace(
        session_factory,
        title="Wrong base",
        branch_base="development",
        owned_paths=["src/awf/api.py"],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=9),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    node_ids = {node.workspace_id for node in graph.nodes}
    assert excluded_ids.isdisjoint(node_ids)
    assert {active_left_id, active_right_id, wrong_repo_id, wrong_base_id}.issubset(node_ids)
    assert len(graph.edges) == 1
    assert graph.edges[0].affected_workspace_ids == tuple(sorted([active_left_id, active_right_id]))


@pytest.mark.unit
async def test_overlap_graph_path_match_explanations_cover_exact_ancestor_and_wildcard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    left_id = await _workspace(
        session_factory,
        title="Path left",
        owned_paths=["README.md", "src/awf", "tests/**", "apps/console/**"],
        status=WorkspaceStatus.ready,
        created_at=now,
    )
    right_id = await _workspace(
        session_factory,
        title="Path right",
        owned_paths=[
            "./README.md",
            "src/awf/api/routes.py",
            "tests/unit/test_api.py",
            "apps/**",
        ],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert len(graph.edges) == 1
    matches = graph.edges[0].path_matches
    assert {(match.left_workspace_id, match.right_workspace_id) for match in matches} == {
        tuple(sorted([left_id, right_id]))
    }
    assert all(match.explanation for match in matches)
    assert {
        (match.match_reason_code, frozenset([match.left_owned_path, match.right_owned_path]))
        for match in matches
    } == {
        ("OWNED_PATH_EXACT_MATCH", frozenset(["README.md", "./README.md"])),
        (
            "OWNED_PATH_ANCESTOR_MATCH",
            frozenset(["src/awf", "src/awf/api/routes.py"]),
        ),
        ("OWNED_PATH_WILDCARD_MATCH", frozenset(["tests/**", "tests/unit/test_api.py"])),
        ("OWNED_PATH_WILDCARD_MATCH", frozenset(["apps/console/**", "apps/**"])),
    }


@pytest.mark.unit
async def test_overlap_graph_queue_state_filter_limits_nodes_before_edges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    queued_ids = {
        await _workspace(
            session_factory,
            title="Queued one",
            repo_url="git@github.com:example/queued.git",
            owned_paths=["queued/**"],
            status=WorkspaceStatus.requested,
            created_at=now,
        ),
        await _workspace(
            session_factory,
            title="Queued two",
            repo_url="git@github.com:example/queued.git",
            owned_paths=["queued/file.py"],
            status=WorkspaceStatus.ready,
            created_at=now + timedelta(minutes=1),
        ),
    }
    running_ids = {
        await _workspace(
            session_factory,
            title="Running one",
            repo_url="git@github.com:example/running.git",
            owned_paths=["running/**"],
            status=WorkspaceStatus.running,
            created_at=now + timedelta(minutes=2),
        ),
        await _workspace(
            session_factory,
            title="Running two",
            repo_url="git@github.com:example/running.git",
            owned_paths=["running/file.py"],
            status=WorkspaceStatus.validating,
            created_at=now + timedelta(minutes=3),
        ),
    }
    mixed_queued_id = await _workspace(
        session_factory,
        title="Mixed queued",
        repo_url="git@github.com:example/mixed.git",
        owned_paths=["mixed/**"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=4),
    )
    mixed_running_id = await _workspace(
        session_factory,
        title="Mixed running",
        repo_url="git@github.com:example/mixed.git",
        owned_paths=["mixed/file.py"],
        status=WorkspaceStatus.pushing,
        created_at=now + timedelta(minutes=5),
    )

    queued_graph = await build_workspace_overlap_graph(session_factory, queue_state="queued")
    running_graph = await build_workspace_overlap_graph(session_factory, queue_state="running")
    all_graph = await build_workspace_overlap_graph(session_factory, queue_state="all")

    assert {node.workspace_id for node in queued_graph.nodes} == queued_ids | {mixed_queued_id}
    assert {edge.affected_workspace_ids for edge in queued_graph.edges} == {
        tuple(sorted(queued_ids))
    }
    assert {node.workspace_id for node in running_graph.nodes} == running_ids | {mixed_running_id}
    assert {edge.affected_workspace_ids for edge in running_graph.edges} == {
        tuple(sorted(running_ids))
    }
    assert tuple(sorted([mixed_queued_id, mixed_running_id])) in {
        edge.affected_workspace_ids for edge in all_graph.edges
    }


@pytest.mark.unit
async def test_overlap_graph_deterministic_and_compact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import build_workspace_overlap_graph

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    older_id = await _workspace(
        session_factory,
        title="Older",
        owned_paths=["src/**", "src/**", "src/awf/**"],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    newer_a_id = await _workspace(
        session_factory,
        title="Newer A",
        owned_paths=["src/awf/service.py", "src/awf/service.py"],
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=1),
    )
    newer_b_id = await _workspace(
        session_factory,
        title="Newer B",
        owned_paths=["docs/**"],
        status=WorkspaceStatus.requested,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert [node.workspace_id for node in graph.nodes] == [
        *sorted([newer_a_id, newer_b_id], reverse=True),
        older_id,
    ]
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.affected_workspace_ids == tuple(sorted([older_id, newer_a_id]))
    assert {
        frozenset([match.left_owned_path, match.right_owned_path]) for match in edge.path_matches
    } == {
        frozenset(["src/**", "src/awf/service.py"]),
        frozenset(["src/awf/**", "src/awf/service.py"]),
    }


@pytest.mark.unit
async def test_overlap_graph_caps_path_matches_per_edge(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.overlap_graph import (
        OVERLAP_GRAPH_PATH_MATCH_LIMIT,
        build_workspace_overlap_graph,
    )

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    path_count = OVERLAP_GRAPH_PATH_MATCH_LIMIT + 2
    left_id = await _workspace(
        session_factory,
        title="Broad left",
        owned_paths=[f"src/pkg{index}/**" for index in range(path_count)],
        status=WorkspaceStatus.running,
        created_at=now,
    )
    right_id = await _workspace(
        session_factory,
        title="Broad right",
        owned_paths=[f"src/pkg{index}/feature.py" for index in range(path_count)],
        status=WorkspaceStatus.ready,
        created_at=now + timedelta(minutes=1),
    )

    graph = await build_workspace_overlap_graph(session_factory)

    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.affected_workspace_ids == tuple(sorted([left_id, right_id]))
    assert edge.path_match_count == path_count
    assert edge.path_matches_truncated is True
    assert len(edge.path_matches) == OVERLAP_GRAPH_PATH_MATCH_LIMIT


@pytest.mark.unit
async def test_overlap_graph_offloads_pairwise_path_comparison(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import overlap_graph

    offloaded = False

    async def fake_to_thread(function, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal offloaded
        offloaded = True
        return function(*args, **kwargs)

    monkeypatch.setattr(overlap_graph.asyncio, "to_thread", fake_to_thread)

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await _workspace(
        session_factory,
        title="Left",
        owned_paths=["src/**"],
        status=WorkspaceStatus.ready,
        created_at=now,
    )
    await _workspace(
        session_factory,
        title="Right",
        owned_paths=["src/app.py"],
        status=WorkspaceStatus.running,
        created_at=now + timedelta(minutes=1),
    )

    graph = await overlap_graph.build_workspace_overlap_graph(session_factory)

    assert offloaded is True
    assert len(graph.edges) == 1
