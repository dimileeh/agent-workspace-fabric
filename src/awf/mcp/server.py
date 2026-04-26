"""MCP server surface for AWF.

Builds a ``FastMCP`` instance with create, read, wait, and observability
tools that mirror the REST API.
Because both the MCP tools and the REST handlers want the same underlying
logic (create workspace in DB, fetch by id, etc.) we expose a small
``WorkspaceService`` façade that both can call.

Tool names are prefixed ``awf_`` so Claude Code / Codex can namespace them
cleanly when they show up alongside other MCP servers.
"""

from __future__ import annotations

from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from awf.api.schemas import ErrorResponse, WorkspaceCreateRequest, WorkspaceCreateV2Request
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.profiles.resolver import ProfileResolutionError
from awf.service.workspaces import WorkspaceService

# ── MCP tool registration ─────────────────────────────────────────────────


def build_mcp_server(
    *,
    service: WorkspaceService,
    name: str = "awf",
    instructions: str | None = None,
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
            "(checkout → agent CLI → tests → PR). Poll via awf_get_workspace."
        ),
    )

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
        task_external_id: str | None = Field(
            default=None, description="Optional caller-side task ID for correlation."
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
    ) -> dict[str, Any]:
        """Create a new AWF workspace using the clean v2 contract."""
        req = WorkspaceCreateV2Request(
            repo={"url": repo_url, "base_branch": base_branch},
            task={
                "title": task_title,
                "prompt": task_prompt,
                "kind": task_kind,
                "agent": agent,
                "external_id": task_external_id,
                "auto_merge": auto_merge,
                "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
            },
            workspace={"profile_ref": profile_ref, "profile": profile},
            validation={"commands": validation_commands, "requested_tier": requested_tier},
        )
        try:
            ws = await service.create_v2(req)
        except ProfileResolutionError as exc:
            error = ErrorResponse(error_code="INVALID_PROFILE", message=str(exc))
            return cast(
                dict[str, Any],
                CallToolResult(
                    content=[TextContent(type="text", text=error.model_dump_json())],
                    structuredContent=error.model_dump(mode="json"),
                    isError=True,
                ),
            )
        return ws.model_dump(mode="json")

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
        return result.model_dump(mode="json") if result is not None else None

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

    return mcp
