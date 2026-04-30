"""MCP server surface for AWF.

Builds a ``FastMCP`` instance with create, read, wait, observability, and
operator-control tools that mirror the REST API.
Because both the MCP tools and the REST handlers want the same underlying
logic (create workspace in DB, fetch by id, etc.) we expose a small
``WorkspaceService`` façade that both can call.

Tool names are prefixed ``awf_`` so Claude Code / Codex can namespace them
cleanly when they show up alongside other MCP servers.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.routes import metrics as metrics_routes
from awf.api.schemas import (
    ErrorResponse,
    OperationListResponse,
    OperationResponse,
    OwnedPath,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceLockListResponse,
    WorkspaceLockResponse,
    WorkspaceOverlapGraphResponse,
)
from awf.common.config import Settings, get_settings
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.profiles.resolver import ProfileResolutionError
from awf.service.artifacts import list_workspace_artifacts_metadata
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from awf.service.locks import InvalidWorkspaceLockCursorError, list_workspace_lock_page_for_session
from awf.service.merge_queue import InvalidMergeQueueCursorError, list_merge_queue_response
from awf.service.metrics import (
    DEFAULT_FAILURE_EXAMPLE_LIMIT,
    DEFAULT_SUMMARY_WINDOW_HOURS,
    MAX_FAILURE_EXAMPLE_LIMIT,
    MAX_SUMMARY_WINDOW_HOURS,
    MIN_FAILURE_EXAMPLE_LIMIT,
    MIN_SUMMARY_WINDOW_HOURS,
    summarize_failure_analysis_for_session,
    summarize_resource_saturation_for_session,
    summarize_slo_metrics_for_session,
    summarize_workspace_reliability_for_session,
)
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.overlap_graph import OverlapGraphQueueState, build_workspace_overlap_graph
from awf.service.provider_readiness import ProviderName
from awf.service.tasks import build_task_attempt_list_response, build_task_list_response
from awf.service.validation_provenance import list_validation_provenance_response
from awf.service.workspace_observability import (
    InvalidWorkspaceOverviewCursorError,
    list_workspace_overview_response,
    list_workspace_stale_reasons_response,
)
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary
from awf.service.workspaces import WorkspaceService

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]
DiskCheckProvider = Callable[[Settings], DiskCheck | Awaitable[DiskCheck]]
OrphanResourceSummaryProvider = Callable[
    [Settings, AsyncSession],
    OrphanResourceSummary | Awaitable[OrphanResourceSummary],
]
RuntimeHealthSummaryProvider = Callable[
    [Settings, AsyncSession, OrphanResourceSummary],
    WorkspaceRuntimeHealthSummary | Awaitable[WorkspaceRuntimeHealthSummary],
]
ReadinessProvider = Callable[
    [Settings],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
HealthProvider = Callable[
    [],
    dict[str, Any] | Awaitable[dict[str, Any]],
]


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


# ── MCP tool registration ─────────────────────────────────────────────────


def build_mcp_server(
    *,
    service: WorkspaceService,
    name: str = "awf",
    instructions: str | None = None,
    settings: Settings | None = None,
    disk_check_provider: DiskCheckProvider | None = None,
    orphan_resource_summary_provider: OrphanResourceSummaryProvider | None = None,
    runtime_health_summary_provider: RuntimeHealthSummaryProvider | None = None,
    readiness_provider: ReadinessProvider | None = None,
    health_provider: HealthProvider | None = None,
) -> FastMCP:
    """Construct a FastMCP instance with AWF's tools bound to ``service``.

    The service is captured in closures rather than pulled from a framework
    context var — keeps MCP tools testable by constructing a throwaway
    FastMCP per test with a service over an in-memory SQLite factory.
    """
    mcp = FastMCP(
        name=name,
        instructions=(
            instructions
            or "AWF: isolated Docker execution substrate for coding agents. "
            "Create a workspace to run one coding task end-to-end "
            "(checkout → agent CLI → tests → PR). Poll via awf_get_workspace. "
            "Operator controls cancel, stop, or destroy AWF-managed workspaces; "
            "they are not shell access to workspace containers."
        ),
    )
    settings_value = _resolve_settings(settings)

    @mcp.tool(name="awf_create_workspace")
    async def awf_create_workspace(
        repo_url: str = Field(..., description="Git URL the workspace should check out."),
        task_title: str = Field(..., description="Short title of the task (≤ 512 chars)."),
        task_prompt: str = Field(..., description="Full prompt to hand to the coding CLI."),
        branch_base: str = Field(
            default="development",
            description="Branch to branch FROM; feature branch is created off it.",
        ),
        agent: AgentRuntime = Field(
            default=AgentRuntime.codex,
            description="Which coding CLI to run inside the container.",
        ),
        test_commands: list[str] = Field(
            default_factory=list,
            description="Shell commands to validate the change (e.g. ['pytest -q']).",
        ),
        requires_database: bool = Field(
            default=False,
            description="If True, AWF runs `alembic upgrade head` before test_commands.",
        ),
        env_profile: str | None = Field(
            default=None, description="Optional named env profile (e.g. 'aira-dev')."
        ),
        task_external_id: str | None = Field(
            default=None, description="Optional caller-side task ID for correlation."
        ),
    ) -> dict[str, Any]:
        """Create a new AWF workspace. Returns the initial workspace state (async)."""
        req = WorkspaceCreateRequest(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=task_title,
            task_prompt=task_prompt,
            agent=agent,
            test_commands=test_commands,
            requires_database=requires_database,
            env_profile=env_profile,
            task_external_id=task_external_id,
        )
        return (await service.create(req)).model_dump(mode="json")

    @mcp.tool(name="awf_create_workspace_v2")
    async def awf_create_workspace_v2(
        repo_url: str = Field(..., description="Git URL the workspace should check out."),
        base_branch: str = Field(
            default="main",
            description="Branch to branch FROM; feature branch is created off it.",
        ),
        task_title: str = Field(..., description="Short title of the task (≤ 512 chars)."),
        task_prompt: str = Field(..., description="Full prompt to hand to the coding CLI."),
        task_kind: str = Field(
            default="feature_branch_pr",
            description="Task kind for scheduling/monitor behavior.",
        ),
        agent: AgentRuntime = Field(
            default=AgentRuntime.codex,
            description="Which coding CLI to run inside the container.",
        ),
        model: str | None = Field(
            default=None,
            description="Optional model override for the selected agent runtime.",
        ),
        task_external_id: str | None = Field(
            default=None, description="Optional caller-side task ID for correlation."
        ),
        task_class: TaskClass | None = Field(
            default=None,
            description="Optional PRD policy class for scheduling and overlap-risk policy.",
        ),
        owned_paths: list[OwnedPath] = Field(
            default_factory=list,
            max_length=128,
            description="Optional path globs/strings the task expects to own.",
        ),
        profile_ref: str | None = Field(
            default="auto",
            description="Workspace profile reference, e.g. auto, python, node, aira.",
        ),
        profile: dict[str, Any] | None = Field(
            default=None,
            description="Optional inline workspace profile dictionary.",
        ),
        validation_commands: list[str] = Field(
            default_factory=list,
            description="Shell commands to validate the change.",
        ),
        requested_tier: int = Field(
            default=1,
            ge=1,
            le=3,
            description="Requested validation tier hint.",
        ),
        auto_merge: bool = Field(
            default=True,
            description="Whether AWF may merge once gates are green.",
        ),
        initial_review_grace_period_seconds: float | None = Field(
            default=None,
            ge=0,
            le=86400,
            description="Optional monitor grace override before auto-merge.",
        ),
    ) -> StructuredToolResult:
        """Create a new AWF workspace using the clean v2 contract."""
        req = WorkspaceCreateV2Request(
            repo={"url": repo_url, "base_branch": base_branch},
            task={
                "title": task_title,
                "prompt": task_prompt,
                "kind": task_kind,
                "agent": agent,
                "model": model,
                "external_id": task_external_id,
                "task_class": task_class,
                "owned_paths": owned_paths,
                "auto_merge": auto_merge,
                "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
            },
            workspace={"profile_ref": profile_ref, "profile": profile},
            validation={"commands": validation_commands, "requested_tier": requested_tier},
        )
        try:
            ws = await service.create_v2(req)
        except ProfileResolutionError as exc:
            error = ErrorResponse(
                error_code="INVALID_PROFILE",
                message=str(exc),
                detail=exc.detail,
            )
            return _tool_result(error.model_dump(mode="json"), is_error=True)
        return _tool_result(ws.model_dump(mode="json"))

    @mcp.tool(name="awf_get_workspace")
    async def awf_get_workspace(
        workspace_id: str = Field(..., description="ID returned by awf_create_workspace."),
    ) -> dict[str, Any] | None:
        """Fetch the current state + metadata of one workspace."""
        result = await service.get(workspace_id)
        return result.model_dump(mode="json") if result is not None else None

    @mcp.tool(name="awf_list_workspaces")
    async def awf_list_workspaces(
        limit: int = Field(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """List workspaces, newest first."""
        rows = await service.list(limit=limit)
        return [r.model_dump(mode="json") for r in rows]

    @mcp.tool(name="awf_wait_for_workspace")
    async def awf_wait_for_workspace(
        workspace_id: str = Field(..., description="ID returned by awf_create_workspace."),
        terminal_statuses: list[str] = Field(
            default_factory=lambda: [
                WorkspaceStatus.completed.value,
                WorkspaceStatus.failed.value,
                WorkspaceStatus.cancelled.value,
                WorkspaceStatus.destroyed.value,
            ],
            description="Statuses that end the wait.",
        ),
        poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0),
        timeout_seconds: float = Field(default=1800.0, ge=1.0, le=14400.0),
    ) -> dict[str, Any] | None:
        """Poll until the workspace reaches one of ``terminal_statuses`` or timeout.

        Returns the final workspace state (or None if the workspace vanished).
        The MCP caller can then read pr_url / failure_reason from the payload.
        """
        import asyncio
        import time

        deadline = time.monotonic() + timeout_seconds
        while True:
            ws = await service.get(workspace_id)
            if ws is None:
                return None
            if ws.status.value in terminal_statuses:
                return ws.model_dump(mode="json")
            if time.monotonic() >= deadline:
                return ws.model_dump(mode="json")
            await asyncio.sleep(poll_interval_seconds)

    @mcp.tool(name="awf_cancel_workspace")
    async def awf_cancel_workspace(
        workspace_id: str = Field(..., description="Workspace ID to cancel."),
        reason: str | None = Field(
            default=None,
            description="Optional operator reason to record with the cancellation request.",
        ),
        stop_stack: bool = Field(
            default=True,
            description="Also stop the workspace compose stack after requesting cancellation.",
        ),
    ) -> StructuredToolResult:
        """Operator control: cancel a workspace; this is not shell access."""
        try:
            result = await service.cancel_workspace(
                workspace_id,
                reason=reason,
                stop_stack=stop_stack,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_stop_workspace")
    async def awf_stop_workspace(
        workspace_id: str = Field(..., description="Workspace ID whose stack should stop."),
        reason: str | None = Field(
            default=None,
            description="Optional operator reason to record with the stop request.",
        ),
    ) -> StructuredToolResult:
        """Operator control: stop a workspace stack; this is not shell access."""
        try:
            result = await service.stop_workspace(workspace_id, reason=reason)
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_destroy_workspace")
    async def awf_destroy_workspace(
        workspace_id: str = Field(..., description="Workspace ID to destroy."),
        force: bool = Field(
            default=False,
            description="Required when the workspace is still active.",
        ),
        remove_volumes: bool = Field(
            default=True,
            description="Remove workspace volumes during compose cleanup.",
        ),
        remove_worktree: bool = Field(
            default=True,
            description="Remove the workspace git worktree during cleanup.",
        ),
    ) -> StructuredToolResult:
        """Operator control: destroy workspace resources; this is not shell access."""
        try:
            result = await service.destroy_workspace(
                workspace_id,
                force=force,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_events")
    async def awf_list_workspace_events(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        limit: int = Field(default=50, ge=1, le=500),
        event_type: str | None = Field(default=None, description="Optional event-type filter."),
    ) -> list[dict[str, Any]] | None:
        """List immutable workspace events newest-first."""
        rows = await service.list_events(
            workspace_id,
            limit=limit,
            event_type=event_type,
        )
        return [row.model_dump(mode="json") for row in rows] if rows is not None else None

    @mcp.tool(name="awf_get_workspace_runtime")
    async def awf_get_workspace_runtime(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
    ) -> dict[str, Any] | None:
        """Fetch compose/container runtime state for one workspace."""
        result = await service.get_runtime(workspace_id)
        if result is None:
            return None
        payload = result.model_dump(mode="json")
        if payload.get("runtime_health") is None:
            payload.pop("runtime_health", None)
        return payload

    @mcp.tool(name="awf_list_workspace_operations")
    async def awf_list_workspace_operations(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        limit: int = Field(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]] | None:
        """List one workspace's operations newest-first."""
        rows = await service.list_operations(workspace_id, limit=limit)
        return [row.model_dump(mode="json") for row in rows] if rows is not None else None

    @mcp.tool(name="awf_list_workspace_logs")
    async def awf_list_workspace_logs(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
    ) -> list[dict[str, Any]] | None:
        """List indexed durable log streams for one workspace."""
        rows = await service.list_logs(workspace_id)
        return [row.model_dump(mode="json") for row in rows] if rows is not None else None

    @mcp.tool(name="awf_list_merge_queue")
    async def awf_list_merge_queue(
        repo_url: str | None = Field(default=None, min_length=1, max_length=512),
        base_branch: str | None = Field(default=None, min_length=1, max_length=256),
        workspace_status: WorkspaceStatus | None = Field(default=None),
        limit: int = Field(default=50, ge=1, le=500),
        cursor: str | None = Field(default=None, max_length=128),
    ) -> StructuredToolResult:
        """Read-only operator observability: list the REST merge queue envelope."""
        async with service.session_factory() as session:
            try:
                response = await list_merge_queue_response(
                    session,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    workspace_status=workspace_status,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidMergeQueueCursorError:
                return _error_result("INVALID_CURSOR", "Invalid merge queue cursor.")
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_overview")
    async def awf_list_workspace_overview(
        workspace_status: WorkspaceStatus | None = Field(default=None),
        agent: AgentRuntime | None = Field(default=None),
        repo_url: str | None = Field(default=None, min_length=1, max_length=512),
        limit: int = Field(default=50, ge=1, le=500),
        cursor: str | None = Field(default=None, max_length=128),
    ) -> StructuredToolResult:
        """Read-only operator observability: list the REST workspace overview envelope."""
        async with service.session_factory() as session:
            try:
                response = await list_workspace_overview_response(
                    session,
                    workspace_status=workspace_status,
                    agent=agent,
                    repo_url=repo_url,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidWorkspaceOverviewCursorError:
                return _error_result("INVALID_CURSOR", "Invalid workspace overview cursor.")
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_validation")
    async def awf_list_workspace_validation(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
    ) -> CallToolResult:
        """Read-only operator observability: list validation provenance for a workspace."""
        async with service.session_factory() as session:
            response = await list_validation_provenance_response(
                session,
                workspace_id=workspace_id,
            )
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_stale_reasons")
    async def awf_list_workspace_stale_reasons(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        include_resolved: bool = Field(default=False),
    ) -> CallToolResult:
        """Read-only operator observability: list structured workspace stale reasons."""
        async with service.session_factory() as session:
            response = await list_workspace_stale_reasons_response(
                session,
                workspace_id=workspace_id,
                include_resolved=include_resolved,
            )
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_artifacts")
    async def awf_list_workspace_artifacts(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
    ) -> CallToolResult:
        """Read-only operator observability: list workspace artifact metadata only."""
        async with service.session_factory() as session:
            response = await list_workspace_artifacts_metadata(
                session,
                workspace_id=workspace_id,
                work_dir=settings_value.work_dir,
            )
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_failure_analysis_summary")
    async def awf_get_failure_analysis_summary(
        since_hours: int = Field(
            default=DEFAULT_SUMMARY_WINDOW_HOURS,
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
        limit: int = Field(
            default=DEFAULT_FAILURE_EXAMPLE_LIMIT,
            ge=MIN_FAILURE_EXAMPLE_LIMIT,
            le=MAX_FAILURE_EXAMPLE_LIMIT,
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: summarize workspace failure analysis."""
        async with service.session_factory() as session:
            summary = await summarize_failure_analysis_for_session(
                session,
                since_hours=since_hours,
                failure_example_limit=limit,
            )
        response = metrics_routes.FailureAnalysisSummaryResponse.model_validate(summary)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_workspace_reliability_summary")
    async def awf_get_workspace_reliability_summary(
        since_hours: int = Field(
            default=DEFAULT_SUMMARY_WINDOW_HOURS,
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: summarize workspace reliability metrics."""
        async with service.session_factory() as session:
            summary = await summarize_workspace_reliability_for_session(
                session,
                settings=settings_value,
                since_hours=since_hours,
            )
        response = metrics_routes.WorkspaceReliabilitySummaryResponse.model_validate(summary)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_resource_saturation_summary")
    async def awf_get_resource_saturation_summary() -> StructuredToolResult:
        """Read-only operator observability: summarize resource saturation and admission."""
        async with service.session_factory() as session:
            disk_check = await _provided_disk_check(
                disk_check_provider=disk_check_provider,
                settings=settings_value,
            )
            orphan_resources = await _provided_orphan_resources(
                orphan_resource_summary_provider=orphan_resource_summary_provider,
                settings=settings_value,
                session=session,
            )
            runtime_health = await _provided_runtime_health(
                runtime_health_summary_provider=runtime_health_summary_provider,
                settings=settings_value,
                session=session,
                orphan_resources=orphan_resources,
            )
            summary = await summarize_resource_saturation_for_session(
                session,
                settings=settings_value,
                disk_check=disk_check,
                orphan_resources=orphan_resources,
                runtime_health=runtime_health,
            )
        response = metrics_routes.ResourceSaturationSummaryResponse.model_validate(summary)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_slo_metrics_summary")
    async def awf_get_slo_metrics_summary(
        since_hours: int = Field(
            default=DEFAULT_SUMMARY_WINDOW_HOURS,
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: summarize SLO metrics."""
        async with service.session_factory() as session:
            summary = await summarize_slo_metrics_for_session(
                session,
                settings=settings_value,
                since_hours=since_hours,
            )
        response = metrics_routes.SloMetricsSummaryResponse.model_validate(summary)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_operations")
    async def awf_list_operations(
        workspace_id: str | None = Field(default=None),
        status: OperationStatus | None = Field(default=None),
        operation_type: OperationType | None = Field(default=None),
        limit: int = Field(default=50, ge=1, le=500),
    ) -> StructuredToolResult:
        """Read-only operator observability: list operations using the REST envelope."""
        rows = await service.list_all_operations(
            workspace_id=workspace_id,
            status=status,
            operation_type=operation_type,
            limit=limit + 1,
        )
        response = _operation_list_response(rows, limit=limit)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_operation")
    async def awf_get_operation(
        operation_id: str = Field(..., description="Operation ID to inspect."),
    ) -> CallToolResult:
        """Read-only operator observability: fetch one operation by id."""
        result = await service.get_operation(operation_id)
        if result is None:
            return _null_tool_result()
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_get_overlap_graph")
    async def awf_get_overlap_graph(
        repo_url: str | None = Field(default=None, min_length=1, max_length=512),
        base_branch: str | None = Field(default=None, min_length=1, max_length=256),
        task_class: TaskClass | None = Field(default=None),
        queue_state: OverlapGraphQueueState = Field(default="all"),
        limit: int = Field(default=100, ge=1, le=500),
    ) -> StructuredToolResult:
        """Read-only operator observability: return the advisory owned-path overlap graph."""
        graph = await build_workspace_overlap_graph(
            service.session_factory,
            repo_url=repo_url,
            base_branch=base_branch,
            task_class=task_class,
            queue_state=queue_state,
            limit=limit,
        )
        response = WorkspaceOverlapGraphResponse.model_validate(graph)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_read_workspace_log")
    async def awf_read_workspace_log(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        stream_id: str = Field(..., description="Stream ID from awf_list_workspace_logs."),
        offset: int = Field(default=0, ge=0, description="Byte offset to start reading from."),
        limit_bytes: int = Field(
            default=65_536,
            ge=1,
            le=1_048_576,
            description="Maximum bytes to read.",
        ),
    ) -> dict[str, Any] | None:
        """Read a bounded chunk from an indexed durable log stream."""
        return await service.read_log(
            workspace_id,
            stream_id,
            offset=offset,
            limit_bytes=limit_bytes,
        )

    @mcp.tool(name="awf_list_tasks")
    async def awf_list_tasks(
        status: WorkspaceStatus | None = Field(
            default=None,
            description="Optional workspace status filter.",
        ),
        agent: AgentRuntime | None = Field(
            default=None,
            description="Optional agent runtime filter.",
        ),
        repo_url: str | None = Field(
            default=None,
            min_length=1,
            max_length=512,
            description="Optional repository URL filter.",
        ),
        limit: int = Field(default=50, ge=1, le=500, description="Maximum items to return."),
    ) -> StructuredToolResult:
        """Read-only operator observability: list tasks with their canonical attempt status."""
        async with service.session_factory() as session:
            response = await build_task_list_response(
                session,
                workspace_status=status,
                agent=agent,
                repo_url=repo_url,
                limit=limit,
            )
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_task_attempts")
    async def awf_list_task_attempts(
        task_ref: str = Field(..., min_length=1, max_length=256, description="Task ID or external reference."),
        limit: int = Field(default=100, ge=1, le=500, description="Maximum attempts to return."),
    ) -> StructuredToolResult:
        """Read-only operator observability: list attempts for a given task."""
        async with service.session_factory() as session:
            response = await build_task_attempt_list_response(
                session,
                task_ref,
                limit=limit,
            )
            if response is None:
                return _error_result("NOT_FOUND", f"No task with ref {task_ref}")
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_locks")
    async def awf_list_locks(
        repo_url: str | None = Field(
            default=None,
            min_length=1,
            max_length=512,
            description="Optional repository URL filter.",
        ),
        task_class: TaskClass | None = Field(
            default=None,
            description="Optional task class filter.",
        ),
        workspace_status: WorkspaceStatus | None = Field(
            default=None,
            description="Optional workspace status filter.",
        ),
        limit: int = Field(default=50, ge=1, le=500, description="Maximum items to return."),
        cursor: str | None = Field(
            default=None,
            max_length=256,
            description="Pagination cursor from a previous response.",
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: list workspace owned-path locks with overlap risks."""
        async with service.session_factory() as session:
            try:
                page = await list_workspace_lock_page_for_session(
                    session,
                    repo_url=repo_url,
                    task_class=task_class,
                    status=workspace_status,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidWorkspaceLockCursorError:
                return _error_result("INVALID_CURSOR", "Invalid lock list cursor.")
        response = WorkspaceLockListResponse(
            items=[WorkspaceLockResponse.model_validate(row) for row in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            limit=limit,
            cursor=cursor,
        )
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_service_readiness")
    async def awf_get_service_readiness(
        providers: list[str] | None = Field(
            default=None,
            description="Optional list of provider names to restrict readiness checks to (e.g. 'github', 'codex', 'claude_code', 'gemini', 'opencode', 'docker'). When set, only these providers affect the overall readiness outcome.",
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: report AWF service readiness checks."""
        from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names

        validated_strict_providers = None
        if providers is not None and len(providers) > 0:
            try:
                validated_strict_providers = validate_provider_names(providers)
            except ProviderReadinessError as exc:
                return _error_result("INVALID_PROVIDERS", str(exc))
        payload = await _provided_readiness(
            readiness_provider=readiness_provider,
            settings=settings_value,
            session_factory=service.session_factory,
            validated_strict_providers=validated_strict_providers,
        )
        return _tool_result(payload)

    @mcp.tool(name="awf_get_service_health")
    async def awf_get_service_health() -> StructuredToolResult:
        """Read-only operator observability: report AWF service liveness."""
        payload = await _provided_health(
            health_provider=health_provider,
        )
        return _tool_result(payload)

    @mcp.tool(name="awf_remonitor_workspace")
    async def awf_remonitor_workspace(
        workspace_id: str = Field(..., min_length=1, max_length=256, description="Workspace ID to remonitor."),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator reason to record with the remonitor request.",
        ),
    ) -> StructuredToolResult:
        """Operator control: re-trigger PR monitor for a workspace; this is not shell access."""
        try:
            result = await service.remonitor_workspace(
                workspace_id,
                reason=reason,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_request_workspace_validation")
    async def awf_request_workspace_validation(
        workspace_id: str = Field(..., min_length=1, max_length=256, description="Workspace ID to validate."),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator reason for re-validation.",
        ),
        requested_tier: int | None = Field(
            default=None,
            ge=1,
            le=3,
            description="Optional validation tier hint.",
        ),
    ) -> StructuredToolResult:
        """Operator control: request workspace re-validation; this is not shell access."""
        try:
            result = await service.request_validate_workspace(
                workspace_id,
                reason=reason,
                requested_tier=requested_tier,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(OperationResponse.model_validate(result).model_dump(mode="json"))

    return mcp


def _tool_error(exc: WorkspaceControlError) -> CallToolResult:
    error = ErrorResponse(error_code=exc.error_code, message=exc.message)
    return _tool_result(error.model_dump(mode="json"), is_error=True)


def _error_result(error_code: str, message: str) -> CallToolResult:
    error = ErrorResponse(error_code=error_code, message=message)
    return _tool_result(error.model_dump(mode="json"), is_error=True)


def _operation_list_response(
    rows: list[OperationResponse],
    *,
    limit: int,
) -> OperationListResponse:
    page_rows = rows[:limit]
    return OperationListResponse(
        items=page_rows,
        next_cursor=None,
        has_more=len(rows) > limit,
        limit=limit,
        cursor=None,
    )


def _null_tool_result() -> CallToolResult:
    return CallToolResult(content=[], structuredContent=None)


async def _provided_disk_check(
    *,
    disk_check_provider: DiskCheckProvider | None,
    settings: Settings,
) -> DiskCheck | None:
    if disk_check_provider is None:
        return None
    result = disk_check_provider(settings)
    if inspect.isawaitable(result):
        return await result
    return result


async def _provided_orphan_resources(
    *,
    orphan_resource_summary_provider: OrphanResourceSummaryProvider | None,
    settings: Settings,
    session: AsyncSession,
) -> OrphanResourceSummary | None:
    if orphan_resource_summary_provider is None:
        return None
    result = orphan_resource_summary_provider(settings, session)
    if inspect.isawaitable(result):
        return await result
    return result


async def _provided_runtime_health(
    *,
    runtime_health_summary_provider: RuntimeHealthSummaryProvider | None,
    settings: Settings,
    session: AsyncSession,
    orphan_resources: OrphanResourceSummary | None,
) -> WorkspaceRuntimeHealthSummary | None:
    if runtime_health_summary_provider is None:
        return None
    if orphan_resources is None:
        return None
    result = runtime_health_summary_provider(settings, session, orphan_resources)
    if inspect.isawaitable(result):
        return await result
    return result


async def _provided_readiness(
    *,
    readiness_provider: ReadinessProvider | None,
    settings: Settings,
    session_factory: Any | None = None,
    validated_strict_providers: set[ProviderName] | None = None,
) -> dict[str, Any]:
    if readiness_provider is not None:
        result = readiness_provider(settings)
        if inspect.isawaitable(result):
            return await result
        return result
    import asyncio

    from awf import __version__
    from awf.api.routes.health import (
        CheckResult,
        ReadyResponse,
        _check_agent_runtime_image,
        _check_db,
        _check_docker_cli,
        _check_docker_compose,
        _check_docker_daemon,
        _check_orphan_resources,
    )
    from awf.common.commands import AsyncioSubprocessRunner
    from awf.service.config import resolve_service_settings
    from awf.service.provider_readiness import collect_agent_readiness

    readiness_kwargs: dict[str, Any] = {}
    if validated_strict_providers is not None:
        readiness_kwargs["validated_strict_providers"] = validated_strict_providers
    runner = AsyncioSubprocessRunner()
    db_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_db(session_factory))
    cli_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_docker_cli(runner))
    daemon_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_docker_daemon(runner)
    )
    compose_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_docker_compose(runner)
    )
    image_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_agent_runtime_image(runner, settings.agent_runtime_image)
    )
    agent_readiness_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
        asyncio.to_thread(
            collect_agent_readiness,
            resolve_service_settings(settings),
            **readiness_kwargs,
        )
    )
    await asyncio.gather(
        db_check_task,
        cli_check_task,
        daemon_check_task,
        compose_check_task,
        image_check_task,
        agent_readiness_task,
    )
    db_check = db_check_task.result()
    cli_check = cli_check_task.result()
    daemon_check = daemon_check_task.result()
    compose_check = compose_check_task.result()
    image_check = image_check_task.result()
    agent_readiness = agent_readiness_task.result()
    orphan_check = await _check_orphan_resources(
        runner=runner,
        factory=session_factory,
        work_dir=settings.work_dir,
        db_check=db_check,
        docker_check=daemon_check,
    )
    checks = {
        "db": db_check,
        "docker_cli": cli_check,
        "docker_daemon": daemon_check,
        "docker_compose": compose_check,
        "agent_runtime_image": image_check,
        "orphan_resources": orphan_check,
    }
    overall_ok = all(c.ok for c in checks.values()) and agent_readiness["status"] == "ok"
    readiness = ReadyResponse(
        service="awf",
        version=__version__,
        status="ok" if overall_ok else "fail",
        checks=checks,
        agent_readiness=agent_readiness,
    )
    return readiness.model_dump(mode="json")


async def _provided_health(
    *,
    health_provider: HealthProvider | None,
) -> dict[str, Any]:
    if health_provider is not None:
        result = health_provider()
        if inspect.isawaitable(result):
            return await result
        return result
    from awf import __version__
    from awf.api.routes.health import HealthResponse

    response = HealthResponse(status="ok", service="awf", version=__version__)
    return response.model_dump(mode="json")


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )
