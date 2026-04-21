"""MCP server surface for AWF.

Builds a ``FastMCP`` instance with five tools that mirror the REST API.
Because both the MCP tools and the REST handlers want the same underlying
logic (create workspace in DB, fetch by id, etc.) we expose a small
``WorkspaceService`` façade that both can call.

Tool names are prefixed ``awf_`` so Claude Code / Codex can namespace them
cleanly when they show up alongside other MCP servers.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest, WorkspaceResponse
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository

# ── Service layer: shared by REST + MCP ───────────────────────────────────


class WorkspaceService:
    """Domain operations shared across the REST router and MCP tools.

    The router + tools stay thin (shape parsing only); the business logic
    lives here so we don't duplicate the repository-and-commit dance.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def create(self, req: WorkspaceCreateRequest) -> WorkspaceResponse:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=req.repo_url,
                branch_base=req.branch_base,
                task_title=req.task_title,
                task_prompt=req.task_prompt,
                task_external_id=req.task_external_id,
                agent=req.agent.value,
                env_profile=req.env_profile,
                test_commands=req.test_commands,
                requires_database=req.requires_database,
            )
            await s.commit()
            return WorkspaceResponse.model_validate(ws)

    async def get(self, workspace_id: str) -> WorkspaceResponse | None:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            return WorkspaceResponse.model_validate(ws) if ws is not None else None

    async def list(self, *, limit: int = 50) -> list[WorkspaceResponse]:
        async with self._factory() as s:
            rows = await WorkspaceRepository(s).list(limit=limit)
            return [WorkspaceResponse.model_validate(r) for r in rows]


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

    return mcp
