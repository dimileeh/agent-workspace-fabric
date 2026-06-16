"""Advisory owned-path overlap graph for active workspaces."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.owned_paths import (
    internal_plan_artifact_owned_paths_from_profile,
    interworkspace_owned_paths,
)
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import owned_path_overlap_match

type OverlapGraphQueueState = Literal["queued", "running", "all"]
type WorkspaceOverlapNodeQueueState = Literal["queued", "running"]

OVERLAP_GRAPH_REASON_CODE: Final = "OWNED_PATH_OVERLAP_RISK"
OVERLAP_GRAPH_SEVERITY: Final = "advisory"
OVERLAP_GRAPH_PATH_MATCH_LIMIT: Final = 8
QUEUED_WORKSPACE_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.requested.value,
    WorkspaceStatus.ready.value,
)
RUNNING_WORKSPACE_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
    # A blocked workspace stays paused with its worktree and owned paths held
    # (keep-warm) awaiting an operator grant/resume decision, so it must surface
    # in the advisory graph — otherwise operators miss overlap risk for exactly
    # the workspaces they are about to grant/resume or start related work
    # against. It maps to the "running" node queue state via the splat below.
    WorkspaceStatus.blocked.value,
)
ACTIVE_OVERLAP_GRAPH_STATUSES: Final[tuple[str, ...]] = (
    *QUEUED_WORKSPACE_STATUSES,
    *RUNNING_WORKSPACE_STATUSES,
)


@dataclass(frozen=True)
class WorkspaceOverlapNode:
    """Workspace node included in the advisory overlap graph."""

    workspace_id: str
    title: str
    status: str
    queue_state: WorkspaceOverlapNodeQueueState
    repo_url: str
    branch_base: str
    task_class: str | None
    owned_paths: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkspaceOverlapPathMatch:
    """One owned-path match explaining an overlap edge."""

    left_workspace_id: str
    left_owned_path: str
    right_workspace_id: str
    right_owned_path: str
    match_reason_code: str
    explanation: str


@dataclass(frozen=True)
class WorkspaceOverlapEdge:
    """Advisory overlap edge between two active workspaces."""

    left_workspace_id: str
    right_workspace_id: str
    repo_url: str
    branch_base: str
    reason_code: str
    severity: str
    blocks_launch: bool
    affected_workspace_ids: tuple[str, str]
    path_match_count: int
    path_matches_truncated: bool
    path_matches: tuple[WorkspaceOverlapPathMatch, ...]


@dataclass(frozen=True)
class WorkspaceOverlapGraphSummary:
    """Aggregate counts for an overlap graph page."""

    node_count: int
    queued_count: int
    running_count: int
    edge_count: int
    affected_workspace_count: int
    has_more: bool


@dataclass(frozen=True)
class WorkspaceOverlapGraph:
    """Advisory graph of active workspace owned-path overlaps."""

    nodes: tuple[WorkspaceOverlapNode, ...]
    edges: tuple[WorkspaceOverlapEdge, ...]
    summary: WorkspaceOverlapGraphSummary


@dataclass(frozen=True)
class _GraphWorkspace:
    workspace_id: str
    title: str
    status: str
    queue_state: WorkspaceOverlapNodeQueueState
    repo_url: str
    branch_base: str
    task_class: str | None
    owned_paths: tuple[str, ...]
    internal_plan_artifact_paths: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class _WorkspaceOverlapPathMatches:
    matches: tuple[WorkspaceOverlapPathMatch, ...]
    total_count: int

    @property
    def truncated(self) -> bool:
        return self.total_count > len(self.matches)


async def build_workspace_overlap_graph(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_url: str | None = None,
    base_branch: str | None = None,
    task_class: TaskClass | str | None = None,
    queue_state: OverlapGraphQueueState = "all",
    limit: int = 100,
) -> WorkspaceOverlapGraph:
    """Build an advisory overlap graph from a session factory."""
    async with session_factory() as session:
        return await build_workspace_overlap_graph_for_session(
            session,
            repo_url=repo_url,
            base_branch=base_branch,
            task_class=task_class,
            queue_state=queue_state,
            limit=limit,
        )


async def build_workspace_overlap_graph_for_session(
    session: AsyncSession,
    *,
    repo_url: str | None = None,
    base_branch: str | None = None,
    task_class: TaskClass | str | None = None,
    queue_state: OverlapGraphQueueState = "all",
    limit: int = 100,
) -> WorkspaceOverlapGraph:
    """Build an advisory overlap graph using an existing session."""
    stmt = select(Workspace).where(
        Workspace.status.in_(_status_values_for_queue_state(queue_state))
    )
    if repo_url is not None:
        stmt = stmt.where(Workspace.repo_url == repo_url)
    if base_branch is not None:
        stmt = stmt.where(Workspace.branch_base == base_branch)
    task_class_value = _task_class_value(task_class)
    if task_class_value is not None:
        stmt = stmt.where(Workspace.task_class == task_class_value)
    stmt = stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc()).limit(limit + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    workspaces = tuple(_graph_workspace(row) for row in page_rows)
    nodes = tuple(_node_from_workspace(workspace) for workspace in workspaces)
    edges = await _workspace_overlap_edges(workspaces)
    return WorkspaceOverlapGraph(
        nodes=nodes,
        edges=edges,
        summary=_graph_summary(nodes, edges, has_more=has_more),
    )


async def _workspace_overlap_edges(
    workspaces: tuple[_GraphWorkspace, ...],
) -> tuple[WorkspaceOverlapEdge, ...]:
    if len(workspaces) < 2:
        return ()
    return await asyncio.to_thread(_workspace_overlap_edges_sync, workspaces)


def _workspace_overlap_edges_sync(
    workspaces: tuple[_GraphWorkspace, ...],
) -> tuple[WorkspaceOverlapEdge, ...]:
    groups: dict[tuple[str, str], list[_GraphWorkspace]] = {}
    for workspace in workspaces:
        groups.setdefault((workspace.repo_url, workspace.branch_base), []).append(workspace)

    edges: list[WorkspaceOverlapEdge] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda workspace: workspace.workspace_id)
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                edge = _workspace_overlap_edge(left, right)
                if edge is not None:
                    edges.append(edge)
    return tuple(
        sorted(
            edges,
            key=lambda edge: (
                edge.repo_url,
                edge.branch_base,
                edge.affected_workspace_ids[0],
                edge.affected_workspace_ids[1],
            ),
        )
    )


def _workspace_overlap_edge(
    left: _GraphWorkspace,
    right: _GraphWorkspace,
) -> WorkspaceOverlapEdge | None:
    path_matches = _workspace_overlap_path_matches(left, right)
    if path_matches.total_count == 0:
        return None

    affected_workspace_ids = (left.workspace_id, right.workspace_id)
    return WorkspaceOverlapEdge(
        left_workspace_id=left.workspace_id,
        right_workspace_id=right.workspace_id,
        repo_url=left.repo_url,
        branch_base=left.branch_base,
        reason_code=OVERLAP_GRAPH_REASON_CODE,
        severity=OVERLAP_GRAPH_SEVERITY,
        blocks_launch=False,
        affected_workspace_ids=affected_workspace_ids,
        path_match_count=path_matches.total_count,
        path_matches_truncated=path_matches.truncated,
        path_matches=path_matches.matches,
    )


def _workspace_overlap_path_matches(
    left: _GraphWorkspace,
    right: _GraphWorkspace,
) -> _WorkspaceOverlapPathMatches:
    """Build bounded path-match details for an advisory workspace edge."""
    matches: list[WorkspaceOverlapPathMatch] = []
    total_count = 0
    left_paths = sorted(
        interworkspace_owned_paths(
            left.owned_paths,
            internal_plan_artifact_paths=left.internal_plan_artifact_paths,
        )
    )
    right_paths = sorted(
        interworkspace_owned_paths(
            right.owned_paths,
            internal_plan_artifact_paths=right.internal_plan_artifact_paths,
        )
    )
    for left_owned_path in left_paths:
        for right_owned_path in right_paths:
            overlap = owned_path_overlap_match(left_owned_path, right_owned_path)
            if overlap is None:
                continue
            total_count += 1
            if len(matches) < OVERLAP_GRAPH_PATH_MATCH_LIMIT:
                matches.append(
                    WorkspaceOverlapPathMatch(
                        left_workspace_id=left.workspace_id,
                        left_owned_path=left_owned_path,
                        right_workspace_id=right.workspace_id,
                        right_owned_path=right_owned_path,
                        match_reason_code=overlap.match_reason_code,
                        explanation=overlap.explanation,
                    )
                )
    return _WorkspaceOverlapPathMatches(
        matches=tuple(
            sorted(
                matches,
                key=lambda match: (
                    match.left_owned_path,
                    match.right_owned_path,
                    match.match_reason_code,
                ),
            )
        ),
        total_count=total_count,
    )


def _graph_workspace(workspace: Workspace) -> _GraphWorkspace:
    return _GraphWorkspace(
        workspace_id=workspace.id,
        title=workspace.task_title,
        status=workspace.status,
        queue_state=_queue_state_for_status(workspace.status),
        repo_url=workspace.repo_url,
        branch_base=workspace.branch_base,
        task_class=workspace.task_class,
        owned_paths=tuple(workspace.owned_paths),
        internal_plan_artifact_paths=internal_plan_artifact_owned_paths_from_profile(
            workspace.resolved_profile,
            workspace_id=workspace.id,
        ),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _node_from_workspace(workspace: _GraphWorkspace) -> WorkspaceOverlapNode:
    return WorkspaceOverlapNode(
        workspace_id=workspace.workspace_id,
        title=workspace.title,
        status=workspace.status,
        queue_state=workspace.queue_state,
        repo_url=workspace.repo_url,
        branch_base=workspace.branch_base,
        task_class=workspace.task_class,
        owned_paths=workspace.owned_paths,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _graph_summary(
    nodes: Iterable[WorkspaceOverlapNode],
    edges: Iterable[WorkspaceOverlapEdge],
    *,
    has_more: bool,
) -> WorkspaceOverlapGraphSummary:
    node_items = tuple(nodes)
    edge_items = tuple(edges)
    affected_workspace_ids = {
        workspace_id for edge in edge_items for workspace_id in edge.affected_workspace_ids
    }
    return WorkspaceOverlapGraphSummary(
        node_count=len(node_items),
        queued_count=sum(1 for node in node_items if node.queue_state == "queued"),
        running_count=sum(1 for node in node_items if node.queue_state == "running"),
        edge_count=len(edge_items),
        affected_workspace_count=len(affected_workspace_ids),
        has_more=has_more,
    )


def _status_values_for_queue_state(
    queue_state: OverlapGraphQueueState,
) -> tuple[str, ...]:
    if queue_state == "queued":
        return QUEUED_WORKSPACE_STATUSES
    if queue_state == "running":
        return RUNNING_WORKSPACE_STATUSES
    return ACTIVE_OVERLAP_GRAPH_STATUSES


def _queue_state_for_status(status: str) -> WorkspaceOverlapNodeQueueState:
    if status in QUEUED_WORKSPACE_STATUSES:
        return "queued"
    if status in RUNNING_WORKSPACE_STATUSES:
        return "running"
    raise ValueError(f"status {status!r} is not part of the overlap graph")


def _task_class_value(task_class: TaskClass | str | None) -> str | None:
    if task_class is None:
        return None
    return task_class.value if isinstance(task_class, TaskClass) else task_class
