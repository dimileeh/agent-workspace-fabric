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

import asyncio
import base64
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import AliasChoices, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.routes import metrics as metrics_routes
from awf.api.schemas import (
    ErrorResponse,
    OperationResponse,
    OwnedPath,
    PullRequestMonitorAdoptionRequest,
    WorkspaceAcceptedResponse,
    WorkspaceArtifactReadResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventListResponse,
    WorkspaceLockListResponse,
    WorkspaceLockResponse,
    WorkspaceLogListResponse,
    WorkspaceLogReadResponse,
    WorkspaceOverlapGraphResponse,
)
from awf.common.audit import redact_audit_text
from awf.common.config import Settings, get_settings
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import TaskExternalIdConflictError, WorkspaceRepository
from awf.profiles.resolver import ProfileResolutionError
from awf.service import config as service_config
from awf.service import provider_readiness as provider_readiness_service
from awf.service.artifacts import (
    DEFAULT_ARTIFACT_LIST_LIMIT,
    MAX_ARTIFACT_LIST_LIMIT,
    ArtifactNotFoundError,
    ArtifactOversizedError,
    ArtifactPathError,
    get_workspace_artifact_content,
    list_workspace_artifacts_metadata,
    workspace_artifact_dir,
)
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.local_capacity import detect_local_capacity
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
from awf.service.operations import build_operation_list_response
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.overlap_graph import OverlapGraphQueueState, build_workspace_overlap_graph
from awf.service.pr_monitor_adoption import PRMonitorAdoptionError
from awf.service.provider_readiness import ProviderName
from awf.service.resource_capacity import LocalCapacityLimits
from awf.service.tasks import build_task_attempt_list_response, build_task_list_response
from awf.service.validation_provenance import (
    DEFAULT_VALIDATION_PROVENANCE_LIMIT,
    MAX_VALIDATION_PROVENANCE_LIMIT,
    list_validation_provenance_response,
)
from awf.service.workspace_observability import (
    DEFAULT_STALE_REASON_LIMIT,
    MAX_STALE_REASON_LIMIT,
    InvalidWorkspaceOverviewCursorError,
    list_workspace_overview_response,
    list_workspace_stale_reasons_response,
)
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary
from awf.service.workspaces import (
    WorkspaceCreateIdempotencyConflictError,
    WorkspaceCreateInsufficientDiskError,
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryError,
    WorkspaceService,
)

if TYPE_CHECKING:
    from awf.service.config import ServiceSettings

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]
DiskCheckProvider = Callable[[Settings], DiskCheck | Awaitable[DiskCheck]]
LocalCapacityProvider = Callable[
    [Settings],
    LocalCapacityLimits | Awaitable[LocalCapacityLimits],
]
OrphanResourceSummaryProvider = Callable[
    [Settings, AsyncSession],
    OrphanResourceSummary | Awaitable[OrphanResourceSummary],
]
RuntimeHealthSummaryProvider = Callable[
    [Settings, AsyncSession, OrphanResourceSummary],
    WorkspaceRuntimeHealthSummary | Awaitable[WorkspaceRuntimeHealthSummary],
]

_IDEMPOTENCY_KEY_REQUIRED_MESSAGE = "Idempotency-Key header is required for this endpoint."
_OPERATION_TYPE_FILTER_ALIAS = AliasChoices("type", "operation_type")


class ReadinessProvider(Protocol):
    """Protocol for readiness-check providers used during workspace admission."""

    def __call__(
        self,
        settings: Settings,
        *,
        validated_strict_providers: set[ProviderName] | None = None,
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        """Invoke the readiness check."""
        ...


HealthProvider = Callable[
    [],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
ProviderFilter = Annotated[str, Field(min_length=1, max_length=64)]


def _resolve_settings(settings: Settings | None) -> Settings:
    """Return *settings* if provided, otherwise resolve the global default."""
    return settings or get_settings()


# ── MCP tool registration ─────────────────────────────────────────────────


def build_mcp_server(
    *,
    service: WorkspaceService,
    name: str = "awf",
    instructions: str | None = None,
    settings: Settings | None = None,
    disk_check_provider: DiskCheckProvider | None = None,
    local_capacity_provider: LocalCapacityProvider | None = None,
    orphan_resource_summary_provider: OrphanResourceSummaryProvider | None = None,
    runtime_health_summary_provider: RuntimeHealthSummaryProvider | None = None,
    readiness_provider: ReadinessProvider | None = None,
    health_provider: HealthProvider | None = None,
) -> FastMCP:
    """Construct a FastMCP instance with AWF's tools bound to ``service``.

    The service is captured in closures rather than pulled from a framework
    context var, which keeps MCP tools testable with an injected service.
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

    def _safe_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
        """Redact sensitive data from *payload* and wrap in a ``CallToolResult``."""
        redacted = _redact_sensitive_payload(payload, settings_value)
        return _tool_result(redacted, is_error=is_error)

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
        idempotency_key: str | None = Field(
            default=None,
            description="Optional replay key matching the REST Idempotency-Key header.",
        ),
    ) -> StructuredToolResult:
        """Create a new AWF workspace. Returns the accepted workspace payload."""
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
        try:
            response = await service.create(
                req,
                idempotency_key=_normalize_mcp_idempotency_key(idempotency_key),
            )
        except WorkspaceCreateIdempotencyConflictError as exc:
            return _workspace_error_result(exc)
        return _tool_result(_workspace_accepted_payload(response))

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
        priority: int | None = Field(default=None, ge=0, le=100, description="Optional priority."),
        human_boost: int | None = Field(
            default=None, ge=0, le=5, description="Optional priority boost."
        ),
        out_of_scope_changes: dict[str, Any] | None = Field(
            default=None,
            description="Optional out-of-scope change policy payload.",
        ),
        provider_recovery: dict[str, Any] | None = Field(
            default=None,
            description="Optional provider-recovery policy payload.",
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
        cpu: float | None = Field(default=None, gt=0, description="Optional CPU request."),
        memory: str | None = Field(
            default=None, max_length=32, description="Optional memory request."
        ),
        steady_state_cpu_cores: float | None = Field(
            default=None, gt=0, description="Optional steady-state CPU."
        ),
        steady_state_memory_gb: float | None = Field(
            default=None, gt=0, description="Optional steady-state memory."
        ),
        peak_cpu_cores: float | None = Field(default=None, gt=0, description="Optional peak CPU."),
        peak_memory_gb: float | None = Field(
            default=None, gt=0, description="Optional peak memory."
        ),
        disk_mb: int | None = Field(default=None, gt=0, description="Optional disk MB request."),
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
        provider_readiness_override: bool = Field(
            default=False,
            description="Explicitly admit launch when selected provider readiness is not ready.",
        ),
        provider_readiness_override_reason: str | None = Field(
            default=None,
            max_length=512,
            description="Audit reason for provider_readiness_override.",
        ),
        idempotency_key: str | None = Field(
            default=None,
            description="Optional replay key matching the REST Idempotency-Key header.",
        ),
    ) -> StructuredToolResult:
        """Create a new AWF workspace using the clean v2 contract."""
        req = WorkspaceCreateV2Request(
            repo={"url": repo_url, "base_branch": base_branch},
            task={
                k: v
                for k, v in {
                    "title": task_title,
                    "prompt": task_prompt,
                    "kind": task_kind,
                    "agent": agent,
                    "model": model,
                    "external_id": task_external_id,
                    "task_class": task_class,
                    "priority": priority,
                    "human_boost": human_boost,
                    "out_of_scope_changes": out_of_scope_changes,
                    "provider_recovery": provider_recovery,
                    "owned_paths": owned_paths,
                    "auto_merge": auto_merge,
                    "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
                }.items()
                if v is not None
            },
            workspace={"profile_ref": profile_ref, "profile": profile},
            validation={"commands": validation_commands, "requested_tier": requested_tier},
            resources={
                k: v
                for k, v in {
                    "cpu": cpu,
                    "memory": memory,
                    "steady_state_cpu_cores": steady_state_cpu_cores,
                    "steady_state_memory_gb": steady_state_memory_gb,
                    "peak_cpu_cores": peak_cpu_cores,
                    "peak_memory_gb": peak_memory_gb,
                    "disk_mb": disk_mb,
                }.items()
                if v is not None
            },
            preflight={
                "provider_readiness_override": provider_readiness_override,
                "provider_readiness_override_reason": provider_readiness_override_reason,
            },
        )

        async def resolve_disk_check() -> DiskCheck:
            """Resolve the disk-check provider for workspace admission gating."""
            return await _workspace_admission_disk_check(
                disk_check_provider=disk_check_provider,
                settings=settings_value,
            )

        try:
            ws = await service.create_v2(
                req,
                idempotency_key=_normalize_mcp_idempotency_key(idempotency_key),
                disk_check_factory=resolve_disk_check,
            )
        except WorkspaceCreateIdempotencyConflictError as exc:
            return _workspace_error_result(exc)
        except WorkspaceCreateInsufficientDiskError as exc:
            return _workspace_error_result(exc)
        except ProfileResolutionError as exc:
            error = ErrorResponse(
                error_code="INVALID_PROFILE",
                message=str(exc),
                detail=exc.detail,
            )
            return _tool_result(error.model_dump(mode="json"), is_error=True)
        except TaskExternalIdConflictError as exc:
            return _task_external_id_conflict_result(exc)
        except WorkspaceProviderReadinessBlockedError as exc:
            return _provider_readiness_blocked_result(exc)
        return _tool_result(_workspace_accepted_payload(ws))

    @mcp.tool(name="awf_retry_workspace")
    async def awf_retry_workspace(
        workspace_id: str = Field(..., description="Failed or cancelled workspace ID to retry."),
        provider_readiness_override: bool = Field(
            default=False,
            description="Explicitly admit retry when selected provider readiness is not ready.",
        ),
        provider_readiness_override_reason: str | None = Field(
            default=None,
            max_length=512,
            description="Audit reason for provider_readiness_override.",
        ),
    ) -> StructuredToolResult:
        """Retry a failed or cancelled workspace as a fresh attempt."""
        try:
            response = await service.retry_workspace(
                workspace_id,
                provider_readiness_override=provider_readiness_override,
                provider_readiness_override_reason=provider_readiness_override_reason,
            )
        except WorkspaceProviderReadinessBlockedError as exc:
            return _provider_readiness_blocked_result(exc)
        except WorkspaceRetryError as exc:
            return _workspace_retry_error_result(exc)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_get_workspace")
    async def awf_get_workspace(
        workspace_id: str = Field(..., description="ID returned by awf_create_workspace."),
    ) -> dict[str, Any] | None:
        """Fetch the current state + metadata of one workspace."""
        result = await service.get(workspace_id)
        return result.model_dump(mode="json") if result is not None else None

    @mcp.tool(name="awf_list_workspaces")
    async def awf_list_workspaces(
        status: list[WorkspaceStatus] | WorkspaceStatus | None = Field(
            default=None,
            description="Optional workspace status filter. Can be a single status or a list of statuses.",
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
        limit: int = Field(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """List workspaces, newest first."""
        rows = await service.list(
            workspace_status=status,
            agent=agent,
            repo_url=repo_url,
            limit=limit,
        )
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
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: cancel a workspace; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.cancel_workspace(
                workspace_id,
                reason=reason,
                stop_stack=stop_stack,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
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
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: stop a workspace stack; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.stop_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
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
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: destroy workspace resources; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.destroy_workspace(
                workspace_id,
                force=force,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_events")
    async def awf_list_workspace_events(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        limit: int = Field(default=50, ge=1, le=500),
        event_type: str | None = Field(
            default=None,
            description="Optional event-type filter.",
            min_length=1,
            max_length=64,
        ),
    ) -> CallToolResult:
        """Read-only operator observability: list workspace events with REST envelope."""
        rows = await service.list_events(
            workspace_id,
            limit=limit + 1,
            event_type=event_type,
        )
        if rows is None:
            return _null_tool_result()
        has_more = len(rows) > limit
        items = rows[:limit]
        response = WorkspaceEventListResponse(
            items=items,
            has_more=has_more,
            limit=limit,
            cursor=None,
        )
        return _safe_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_events")
    async def awf_list_events(
        workspace_id: str | None = Field(
            default=None,
            description="Optional workspace ID filter.",
        ),
        event_type: str | None = Field(
            default=None,
            description="Optional event-type filter.",
            min_length=1,
            max_length=64,
        ),
        limit: int = Field(default=50, ge=1, le=500),
    ) -> StructuredToolResult:
        """Read-only operator observability: list global AWF events with REST envelope."""
        rows = await service.list_global_events(
            workspace_id=workspace_id,
            event_type=event_type,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        response = WorkspaceEventListResponse(
            items=items,
            has_more=has_more,
            limit=limit,
            cursor=None,
        )
        return _safe_result(response.model_dump(mode="json"))

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
        status: OperationStatus | None = Field(default=None),
        operation_type: OperationType | None = Field(
            default=None,
            validation_alias=_OPERATION_TYPE_FILTER_ALIAS,
            serialization_alias="type",
        ),
        cursor: str | None = Field(default=None, max_length=128),
    ) -> CallToolResult:
        """List one workspace's operations using the REST envelope."""
        try:
            page = await service.list_operations_page(
                workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit + 1,
                cursor=cursor,
            )
        except InvalidBoundedListCursorError:
            return _error_result("INVALID_CURSOR", "Invalid operation list cursor.")
        if page is None:
            return _error_result("NOT_FOUND", f"No workspace with id {workspace_id}")
        response = build_operation_list_response(
            page.rows,
            limit=limit,
            cursor=cursor,
        )
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_logs")
    async def awf_list_workspace_logs(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
    ) -> CallToolResult:
        """List indexed durable log streams for one workspace using the REST envelope."""
        rows = await service.list_logs(workspace_id)
        if rows is None:
            return _null_tool_result()
        response = WorkspaceLogListResponse(
            items=rows,
            limit=len(rows),
            cursor=None,
        )
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_merge_queue")
    async def awf_list_merge_queue(
        repo_url: str | None = Field(default=None, min_length=1, max_length=512),
        base_branch: str | None = Field(default=None, min_length=1, max_length=256),
        status: WorkspaceStatus | None = Field(default=None),
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
                    workspace_status=status,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidMergeQueueCursorError:
                return _error_result("INVALID_CURSOR", "Invalid merge queue cursor.")
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_overview")
    async def awf_list_workspace_overview(
        status: WorkspaceStatus | None = Field(default=None),
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
                    workspace_status=status,
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
        limit: int = Field(
            default=DEFAULT_VALIDATION_PROVENANCE_LIMIT,
            ge=1,
            le=MAX_VALIDATION_PROVENANCE_LIMIT,
            description="Maximum validation provenance records to return.",
        ),
        cursor: str | None = Field(default=None, max_length=64),
    ) -> CallToolResult:
        """Read-only operator observability: list validation provenance for a workspace."""
        async with service.session_factory() as session:
            try:
                response = await list_validation_provenance_response(
                    session,
                    workspace_id=workspace_id,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidBoundedListCursorError:
                return _error_result("INVALID_CURSOR", "Invalid validation provenance cursor.")
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_stale_reasons")
    async def awf_list_workspace_stale_reasons(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        include_resolved: bool = Field(default=False),
        limit: int = Field(
            default=DEFAULT_STALE_REASON_LIMIT,
            ge=1,
            le=MAX_STALE_REASON_LIMIT,
            description="Maximum stale reason records to return.",
        ),
        cursor: str | None = Field(default=None, max_length=64),
    ) -> CallToolResult:
        """Read-only operator observability: list structured workspace stale reasons."""
        async with service.session_factory() as session:
            try:
                response = await list_workspace_stale_reasons_response(
                    session,
                    workspace_id=workspace_id,
                    include_resolved=include_resolved,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidBoundedListCursorError:
                return _error_result("INVALID_CURSOR", "Invalid stale reason cursor.")
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_list_workspace_artifacts")
    async def awf_list_workspace_artifacts(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        limit: int = Field(
            default=DEFAULT_ARTIFACT_LIST_LIMIT,
            ge=1,
            le=MAX_ARTIFACT_LIST_LIMIT,
            description="Maximum artifact metadata records to return.",
        ),
        cursor: str | None = Field(default=None, max_length=64),
    ) -> CallToolResult:
        """Read-only operator observability: list workspace artifact metadata only."""
        async with service.session_factory() as session:
            try:
                response = await list_workspace_artifacts_metadata(
                    session,
                    workspace_id=workspace_id,
                    work_dir=settings_value.work_dir,
                    limit=limit,
                    cursor=cursor,
                )
            except InvalidBoundedListCursorError:
                return _error_result("INVALID_CURSOR", "Invalid artifact list cursor.")
            if response is None:
                return _null_tool_result()
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_read_workspace_artifact")
    async def awf_read_workspace_artifact(
        workspace_id: str = Field(..., description="Workspace ID to inspect."),
        relative_path: str = Field(..., description="Relative POSIX artifact path to read."),
        limit_bytes: int = Field(
            default=65_536,
            ge=1,
            description=(
                "Maximum bytes to read. "
                "Values above the server ceiling (1_048_576) are rejected "
                "with ARTIFACT_OVERSIZED rather than a schema error."
            ),
        ),
    ) -> StructuredToolResult:
        """Read a bounded chunk from a single workspace artifact.

        Returns a JSON envelope with base64-encoded content.
        """
        async with service.session_factory() as session:
            if not await WorkspaceRepository(session).exists(workspace_id):
                return _error_result("NOT_FOUND", f"No workspace with id {workspace_id}")
        artifact_dir = workspace_artifact_dir(settings_value.work_dir, workspace_id)
        _service_settings = service_config.resolve_service_settings(settings_value)
        try:
            name, content_type, _size_bytes, content = await asyncio.to_thread(
                get_workspace_artifact_content,
                workspace_id=workspace_id,
                artifact_dir=artifact_dir,
                relative_path=relative_path,
                limit_bytes=limit_bytes,
            )
        except ArtifactPathError as exc:
            return _error_result(
                "INVALID_ARTIFACT_PATH",
                _redact_sensitive_text(
                    str(exc), settings_value, service_settings=_service_settings
                ),
            )
        except ArtifactNotFoundError:
            return _error_result(
                "NOT_FOUND",
                _redact_sensitive_text(
                    f"No artifact at path {relative_path}",
                    settings_value,
                    service_settings=_service_settings,
                ),
            )
        except ArtifactOversizedError as exc:
            return _error_result(
                error_code="ARTIFACT_OVERSIZED",
                message=_redact_sensitive_text(
                    str(exc),
                    settings_value,
                    service_settings=_service_settings,
                ),
                detail=exc.detail,
            )
        # Redact known secrets from raw artifact bytes before base64-encoding,
        # so secrets cannot leak past the MCP safety boundary inside the
        # encoded content field.  Apply text redaction to text/* MIME types,
        # to other common textual types (e.g. application/json) that may
        # embed secrets, and to any file that decodes cleanly as text without
        # null bytes (covers .env, .log, .yaml, extensionless text files, etc).
        # Binary artifacts cannot meaningfully contain secret strings and a
        # byte-level replacement would silently corrupt them.
        base_type = content_type.split(";")[0].strip().lower()
        is_likely_binary_type = base_type.startswith(
            (
                "image/",
                "audio/",
                "video/",
                "font/",
                "application/pdf",
                "application/zip",
                "application/gzip",
                "application/x-tar",
                "application/java-archive",
                "application/vnd.ms-",
                "application/vnd.openxmlformats-",
                "application/vnd.oasis.opendocument",
            )
        ) or base_type in {
            "application/wasm",
            "application/postscript",
            "application/epub+zip",
            "application/rtf",
            "application/x-msdos-program",
            "application/java-vm",
            "application/vnd.sqlite3",
        }
        is_likely_text = not is_likely_binary_type and b"\x00" not in content
        content, error_result = await asyncio.to_thread(
            _check_and_redact_artifact_content,
            content,
            limit_bytes,
            settings_value,
            _service_settings,
            is_likely_text,
        )
        if error_result is not None:
            return error_result
        response = WorkspaceArtifactReadResponse(
            workspace_id=workspace_id,
            relative_path=relative_path,
            name=name,
            content_type=content_type,
            size_bytes=len(content),
            content=base64.b64encode(content).decode("ascii"),
        )
        payload = response.model_dump(mode="json")
        encoded_content = payload.pop("content")
        redacted_payload = _redact_sensitive_payload(payload, settings_value)
        redacted_payload["content"] = encoded_content
        return _tool_result(redacted_payload)

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
            local_capacity = await _provided_local_capacity(
                local_capacity_provider=local_capacity_provider,
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
                detected_local_capacity=local_capacity,
                orphan_resources=orphan_resources,
                runtime_health=runtime_health,
            )
        response = metrics_routes.ResourceSaturationSummaryResponse.model_validate(summary)
        return _safe_result(response.model_dump(mode="json"))

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
        operation_type: OperationType | None = Field(
            default=None,
            validation_alias=_OPERATION_TYPE_FILTER_ALIAS,
            serialization_alias="type",
        ),
        limit: int = Field(default=50, ge=1, le=500),
        cursor: str | None = Field(default=None, max_length=128),
    ) -> StructuredToolResult:
        """Read-only operator observability: list operations using the REST envelope."""
        try:
            page = await service.list_all_operations_page(
                workspace_id=workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit + 1,
                cursor=cursor,
            )
            response = build_operation_list_response(
                page.rows,
                limit=limit,
                cursor=cursor,
            )
        except InvalidBoundedListCursorError:
            return _error_result("INVALID_CURSOR", "Invalid operation list cursor.")
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
        result = await service.read_log(
            workspace_id,
            stream_id,
            offset=offset,
            limit_bytes=limit_bytes,
        )
        if result is None:
            return None
        return WorkspaceLogReadResponse(
            stream_id=str(result["stream_id"]),
            offset=int(result["offset"]),
            next_offset=int(result["next_offset"]),
            eof=bool(result["eof"]),
            data=str(result["text"]),
        ).model_dump(mode="json")

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
        task_ref: str = Field(
            ..., min_length=1, max_length=256, description="Task ID or external reference."
        ),
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
        status: WorkspaceStatus | None = Field(
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
                    status=status,
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
        providers: list[ProviderFilter] | None = Field(
            default=None,
            max_length=16,
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
        return _safe_result(payload)

    @mcp.tool(name="awf_get_core_release_readiness")
    async def awf_get_core_release_readiness(
        providers: list[ProviderFilter] | None = Field(
            default=None,
            max_length=16,
            description="Optional strict provider names for the release scorecard.",
        ),
        failure_window_hours: int = Field(
            default=24,
            ge=1,
            le=168,
            description="Recent failure-analysis window used by the release gate.",
        ),
        slo_window_hours: int = Field(
            default=168,
            ge=1,
            le=720,
            description="Rolling PRD SLO metrics window used by the release gate.",
        ),
        allow_generic_failures: bool = Field(
            default=False,
            description="Allow generic recent failure reasons with written rationale.",
        ),
        allow_slo_breach: bool = Field(
            default=False,
            description="Allow PRD SLO threshold breaches with written rationale.",
        ),
    ) -> StructuredToolResult:
        """Read-only operator observability: AWF Core local release scorecard."""
        from awf.service.config import resolve_service_settings
        from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
        from awf.service.readiness import collect_core_readiness_report

        validated_strict_providers = None
        if providers is not None and len(providers) > 0:
            try:
                validated_strict_providers = validate_provider_names(providers)
            except ProviderReadinessError as exc:
                return _error_result("INVALID_PROVIDERS", str(exc))
        report = await collect_core_readiness_report(
            settings=resolve_service_settings(settings_value),
            failure_window_hours=failure_window_hours,
            slo_window_hours=slo_window_hours,
            strict_providers=frozenset(validated_strict_providers or ()),
            allow_generic_failures=allow_generic_failures,
            allow_slo_breach=allow_slo_breach,
        )
        return _safe_result(report.to_dict())

    @mcp.tool(name="awf_get_service_health")
    async def awf_get_service_health() -> StructuredToolResult:
        """Read-only operator observability: report AWF service liveness."""
        payload = await _provided_health(
            health_provider=health_provider,
        )
        return _safe_result(payload)

    @mcp.tool(name="awf_adopt_pull_request_monitor")
    async def awf_adopt_pull_request_monitor(
        repo_url: str | None = Field(
            default=None,
            min_length=1,
            max_length=512,
            description="GitHub repo URL. Use with pr_number, or pass pr_url instead.",
        ),
        repo_slug: str | None = Field(
            default=None,
            min_length=1,
            max_length=256,
            description="GitHub owner/repo slug. Use with pr_number, or pass pr_url instead.",
        ),
        pr_number: int | None = Field(
            default=None,
            ge=1,
            description="GitHub pull request number when repo_url or repo_slug is supplied.",
        ),
        pr_url: str | None = Field(
            default=None,
            min_length=1,
            max_length=512,
            description="Full GitHub pull request URL to adopt.",
        ),
        agent: AgentRuntime = Field(
            default=AgentRuntime.codex,
            description="Coding agent runtime used later by the PR monitor for repair work.",
        ),
        model: str | None = Field(
            default=None,
            min_length=1,
            max_length=128,
            description="Optional model override for the selected agent runtime.",
        ),
        effort: str | None = Field(
            default=None,
            min_length=1,
            max_length=64,
            description="Optional reasoning effort override for the selected agent runtime.",
        ),
        profile_ref: str | None = Field(
            default="auto",
            max_length=128,
            description="Workspace profile reference for the adopted monitor workspace.",
        ),
        profile: dict[str, Any] | None = Field(
            default=None,
            description="Optional inline workspace profile dictionary.",
        ),
        auto_merge: bool = Field(
            default=True,
            description="Whether AWF may merge the adopted PR once monitor gates are green.",
        ),
        initial_review_grace_period_seconds: float | None = Field(
            default=None,
            ge=0,
            le=86400,
            description="Optional monitor grace override before auto-merge.",
        ),
        task_title: str | None = Field(
            default=None,
            min_length=1,
            max_length=512,
            description="Optional adopted workspace title.",
        ),
        task_prompt: str | None = Field(
            default=None,
            min_length=1,
            max_length=16384,
            description="Optional adopted workspace prompt.",
        ),
        reason: str | None = Field(
            default=None,
            max_length=512,
            description="Optional operator audit reason.",
        ),
    ) -> StructuredToolResult:
        """Operator control: adopt an existing GitHub PR into AWF monitoring; this is not shell access."""
        try:
            response = await service.adopt_pull_request_monitor(
                PullRequestMonitorAdoptionRequest(
                    repo_url=repo_url,
                    repo_slug=repo_slug,
                    pr_number=pr_number,
                    pr_url=pr_url,
                    agent=agent,
                    model=model,
                    effort=effort,
                    profile_ref=profile_ref,
                    profile=profile,
                    auto_merge=auto_merge,
                    initial_review_grace_period_seconds=(initial_review_grace_period_seconds),
                    task_title=task_title,
                    task_prompt=task_prompt,
                    reason=reason,
                )
            )
        except PRMonitorAdoptionError as exc:
            return _workspace_error_result(exc)
        return _tool_result(response.model_dump(mode="json"))

    @mcp.tool(name="awf_remonitor_workspace")
    async def awf_remonitor_workspace(
        workspace_id: str = Field(
            ..., min_length=1, max_length=256, description="Workspace ID to remonitor."
        ),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator reason to record with the remonitor request.",
        ),
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: re-trigger PR monitor for a workspace; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.remonitor_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(result.model_dump(mode="json"))

    @mcp.tool(name="awf_request_workspace_validation")
    async def awf_request_workspace_validation(
        workspace_id: str = Field(
            ..., min_length=1, max_length=256, description="Workspace ID to validate."
        ),
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
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: request workspace re-validation; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.request_validate_workspace(
                workspace_id,
                reason=reason,
                requested_tier=requested_tier,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(OperationResponse.model_validate(result).model_dump(mode="json"))

    @mcp.tool(name="awf_refresh_workspace")
    async def awf_refresh_workspace(
        workspace_id: str = Field(
            ..., min_length=1, max_length=256, description="Workspace ID to refresh."
        ),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator reason for refresh.",
        ),
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: request workspace refresh; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.request_refresh_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(OperationResponse.model_validate(result).model_dump(mode="json"))

    @mcp.tool(name="awf_rebase_workspace")
    async def awf_rebase_workspace(
        workspace_id: str = Field(
            ..., min_length=1, max_length=256, description="Workspace ID to rebase."
        ),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator reason for rebase.",
        ),
        idempotency_key: str | None = Field(
            ...,
            max_length=128,
            json_schema_extra={"minLength": 1},
            description="Required idempotency key for safe retries after timeout or dropped response.",
        ),
        expected_version: int | None = Field(
            default=None,
            description="Optional optimistic concurrency version (maps to If-Match).",
        ),
    ) -> StructuredToolResult:
        """Operator control: request workspace rebase; this is not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.request_rebase_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key_value,
                expected_version=expected_version,
            )
        except WorkspaceControlError as exc:
            return _tool_error(exc)
        return _tool_result(OperationResponse.model_validate(result).model_dump(mode="json"))

    @mcp.tool(name="awf_get_egress_audit_evidence")
    async def awf_get_egress_audit_evidence(
        workspace_id: str | None = Field(
            default=None,
            max_length=256,
            description="Optional workspace ID filter for egress audit evidence.",
        ),
    ) -> StructuredToolResult:
        """Read-only: return outbound egress audit evidence."""
        workspace_filter = workspace_id.strip() if workspace_id is not None else None
        workspace_filter = workspace_filter or None
        try:
            evidence = await service.get_egress_audit_evidence(workspace_filter)
            return _safe_result({"workspace_id": workspace_filter, "evidence": evidence})
        except Exception as exc:
            return _safe_result(
                {"error_code": "MCP_EGRESS_AUDIT_ERROR", "message": redact_audit_text(str(exc))},
                is_error=True,
            )

    return mcp


def _tool_error(exc: WorkspaceControlError) -> CallToolResult:
    """Convert a ``WorkspaceControlError`` into a structured MCP error result."""
    return _workspace_error_result(exc)


class _WorkspaceErrorSource(Protocol):
    """Protocol for error sources that provide a code, message, and detail."""

    error_code: str
    message: str
    detail: dict[str, Any] | None


def _workspace_retry_error_result(exc: WorkspaceRetryError) -> CallToolResult:
    """Convert a ``WorkspaceRetryError`` into a structured MCP error result."""
    return _workspace_error_result(exc)


def _provider_readiness_blocked_result(
    exc: WorkspaceProviderReadinessBlockedError,
) -> CallToolResult:
    """Convert a provider-readiness preflight failure into a structured MCP error result."""
    return _workspace_error_result(exc)


def _task_external_id_conflict_result(exc: TaskExternalIdConflictError) -> CallToolResult:
    """Return a ``TASK_EXTERNAL_ID_CONFLICT`` error result for duplicate external ID."""
    error = ErrorResponse(
        error_code="TASK_EXTERNAL_ID_CONFLICT",
        message=(
            "External task ID is already associated with a different "
            "repo/base/task-class/owned-path scope; use a unique external "
            "task ID for this backlog slice or retry the original scope."
        ),
        detail={"external_id": exc.external_id},
    )
    return _tool_result(error.model_dump(mode="json"), is_error=True)


def _workspace_error_result(exc: _WorkspaceErrorSource) -> CallToolResult:
    """Convert any workspace error with ``error_code``/``message``/``detail`` into a structured MCP error."""
    error = ErrorResponse(
        error_code=exc.error_code,
        message=exc.message,
        detail=exc.detail,
    )
    return _tool_result(error.model_dump(mode="json"), is_error=True)


def _error_result(
    error_code: str, message: str, *, detail: dict[str, Any] | None = None
) -> CallToolResult:
    """Build a ``CallToolResult`` error from an error code, message, and optional detail."""
    error = ErrorResponse(error_code=error_code, message=message, detail=detail)
    return _tool_result(error.model_dump(mode="json"), is_error=True)


def _required_idempotency_key(idempotency_key: str | None) -> str | None:
    """Return the key if non-empty, otherwise ``None`` to signal a missing required key."""
    if idempotency_key is None:
        return None
    return idempotency_key if idempotency_key.strip() else None


def _normalize_mcp_idempotency_key(idempotency_key: str | None) -> str | None:
    """Strip whitespace from the idempotency key; return ``None`` if blank or absent."""
    if idempotency_key is None:
        return None
    normalized = idempotency_key.strip()
    return normalized or None


def _idempotency_key_error() -> CallToolResult:
    """Return the standard ``INVALID_REQUEST`` error for a missing idempotency key."""
    return _error_result("INVALID_REQUEST", _IDEMPOTENCY_KEY_REQUIRED_MESSAGE)


def _workspace_accepted_payload(ws: Any) -> dict[str, Any]:
    """Extract the accepted-workspace response fields from a workspace object."""
    workspace_id = ws.id
    warnings = [
        warning.model_dump(mode="json") if hasattr(warning, "model_dump") else warning
        for warning in ws.coordination_warnings
    ]
    return WorkspaceAcceptedResponse(
        workspace_id=workspace_id,
        status=ws.status,
        version=ws.version,
        status_url=f"/v1/workspaces/{workspace_id}",
        events_url=f"/v1/workspaces/{workspace_id}/events",
        accepted_at=ws.created_at,
        warnings=warnings,
        provider_readiness_preflight=ws.provider_readiness_preflight,
    ).model_dump(mode="json")


def _null_tool_result() -> CallToolResult:
    """Return an empty ``CallToolResult`` with no content."""
    return CallToolResult(content=[], structuredContent=None)


async def _provided_disk_check(
    *,
    disk_check_provider: DiskCheckProvider | None,
    settings: Settings,
) -> DiskCheck | None:
    """Invoke the injected disk-check provider if given, otherwise return ``None``."""
    if disk_check_provider is None:
        return None
    result = disk_check_provider(settings)
    if inspect.isawaitable(result):
        return await result
    return result


async def _workspace_admission_disk_check(
    *,
    disk_check_provider: DiskCheckProvider | None,
    settings: Settings,
) -> DiskCheck:
    """Resolve a disk-check result, falling back to the real filesystem check."""
    provided = await _provided_disk_check(
        disk_check_provider=disk_check_provider,
        settings=settings,
    )
    if provided is not None:
        return provided
    return await asyncio.to_thread(
        check_disk_space,
        settings.work_dir,
        min_free_bytes=settings.min_free_disk_bytes,
    )


async def _provided_local_capacity(
    *,
    local_capacity_provider: LocalCapacityProvider | None,
    settings: Settings,
) -> LocalCapacityLimits:
    """Invoke the injected local-capacity provider or detect capacity from settings."""
    if local_capacity_provider is not None:
        result = local_capacity_provider(settings)
        if inspect.isawaitable(result):
            return await result
        return result
    if (
        settings.local_capacity_cpu_cores is not None
        and settings.local_capacity_memory_gb is not None
    ):
        return LocalCapacityLimits()
    return await asyncio.to_thread(detect_local_capacity, settings)


async def _provided_orphan_resources(
    *,
    orphan_resource_summary_provider: OrphanResourceSummaryProvider | None,
    settings: Settings,
    session: AsyncSession,
) -> OrphanResourceSummary | None:
    """Invoke the injected orphan-resource summary provider if given."""
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
    """Invoke the injected runtime-health summary provider if given."""
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
    """Invoke the readiness provider or fall back to the built-in readiness check."""
    if readiness_provider is not None:
        result = readiness_provider(
            settings,
            validated_strict_providers=validated_strict_providers,
        )
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
        _check_orphan_resources_with_concurrent_scans,
        _workspace_view_for_readyz,
    )
    from awf.common.commands import AsyncioSubprocessRunner
    from awf.service.config import resolve_service_settings
    from awf.service.orphan_resources import (
        CHECK_TIMEOUT_SECONDS,
        ResourceScan,
        WorkspaceIdView,
        scan_docker_resources_async,
        scan_managed_worktrees,
    )
    from awf.service.provider_readiness import collect_agent_readiness

    readiness_kwargs: dict[str, Any] = {}
    if validated_strict_providers is not None:
        readiness_kwargs["validated_strict_providers"] = validated_strict_providers
    runner = AsyncioSubprocessRunner()
    db_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_db(session_factory))
    cli_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_docker_cli(runner))
    daemon_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_docker_daemon(runner))
    compose_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_docker_compose(runner)
    )
    image_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_agent_runtime_image(runner, settings.agent_runtime_image)
    )
    workspace_view_task: asyncio.Task[WorkspaceIdView] = asyncio.create_task(
        _workspace_view_for_readyz(
            session_factory,
            min_retention_hours=settings.completed_workspace_retention_hours,
        )
    )
    docker_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        scan_docker_resources_async(
            runner=runner,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    )
    worktree_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        asyncio.to_thread(scan_managed_worktrees, settings.work_dir)
    )
    orphan_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_orphan_resources_with_concurrent_scans(
            db_check_task=db_check_task,
            docker_check_task=daemon_check_task,
            workspace_view_task=workspace_view_task,
            docker_scan_task=docker_scan_task,
            worktree_scan_task=worktree_scan_task,
        )
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
        orphan_check_task,
    )
    db_check = db_check_task.result()
    cli_check = cli_check_task.result()
    daemon_check = daemon_check_task.result()
    compose_check = compose_check_task.result()
    image_check = image_check_task.result()
    agent_readiness = agent_readiness_task.result()
    orphan_check = orphan_check_task.result()
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
    """Invoke the health provider or fall back to the built-in health response."""
    if health_provider is not None:
        result = health_provider()
        if inspect.isawaitable(result):
            return await result
        return result
    from awf import __version__
    from awf.api.routes.health import HealthResponse

    response = HealthResponse(status="ok", service="awf", version=__version__)
    return response.model_dump(mode="json")


def _redact_sensitive_payload(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Redact secrets from a JSON payload dict using application settings."""
    service_settings = service_config.resolve_service_settings(settings)
    redacted = _redact_sensitive_value(payload, settings, service_settings=service_settings)
    return redacted if isinstance(redacted, dict) else {}


def _redact_sensitive_value(
    value: Any,
    settings: Settings,
    *,
    service_settings: ServiceSettings,
) -> Any:
    """Recursively redact secrets from an arbitrary value (dict, list, or string)."""
    if isinstance(value, str):
        return _redact_sensitive_text(value, settings, service_settings=service_settings)
    if isinstance(value, list):
        return [
            _redact_sensitive_value(item, settings, service_settings=service_settings)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            _redact_sensitive_text(key, settings, service_settings=service_settings)
            if isinstance(key, str)
            else key: _redact_sensitive_value(item, settings, service_settings=service_settings)
            for key, item in value.items()
        }
    return value


def _contains_secret_bytes(
    content: bytes,
    settings: Settings,
    *,
    service_settings: ServiceSettings,
) -> bool:
    """Check whether binary content contains configured secrets or recognizable token patterns."""
    for secret in (settings.api_token, settings.github_token, service_settings.github_token):
        if secret and len(secret) >= 4 and secret.encode() in content:
            return True
    for key, value in os.environ.items():
        if (
            key.upper() in provider_readiness_service.KNOWN_SECRET_ENV_KEYS
            and len(value) >= 4
            and value.encode() in content
        ):
            return True
    # Also block binary artifacts that contain recognizable provider token
    # patterns (e.g. ghp_..., github_pat_..., sk-proj-...) even when the
    # exact value is not present in current settings or environment.
    decoded = content.decode("latin-1")
    if provider_readiness_service.TOKEN_RE.search(decoded) is not None:
        return True
    # Additionally block URL credentials that the text path would redact.
    return provider_readiness_service.URL_CREDENTIAL_RE.search(decoded) is not None


def _check_and_redact_artifact_content(
    content: bytes,
    limit_bytes: int,
    settings: Settings,
    service_settings: ServiceSettings,
    is_likely_text: bool,
) -> tuple[bytes, CallToolResult | None]:
    """Apply BOM blocking, text redaction, binary secret detection, and size recheck.

    Runs synchronously so it can be off-loaded to ``asyncio.to_thread``.
    Returns ``(content, error_result)`` where ``error_result`` is non-None when
    the artifact must be blocked or is oversized after redaction.
    """
    # Block any artifact that carries a common multibyte text encoding BOM.
    # This must happen before the text-vs-binary dispatch so MIME-less files
    # (e.g. .env, .log, extensionless text) that happen to be UTF-16/UTF-32
    # encoded do not bypass the text redaction path.
    if content.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return (
            b"",
            _error_result(
                error_code="ARTIFACT_BLOCKED",
                message="Artifact uses an unsupported multibyte encoding (UTF-16/UTF-32) and cannot be safely redacted.",
            ),
        )
    if is_likely_text:
        text = content.decode("latin-1")
        redacted_text = _redact_sensitive_text(text, settings, service_settings=service_settings)
        content = redacted_text.encode("latin-1")
    elif _contains_secret_bytes(content, settings, service_settings=service_settings):
        return (
            b"",
            _error_result(
                error_code="ARTIFACT_BLOCKED",
                message="Binary artifact contains configured secrets and cannot be returned.",
            ),
        )
    if len(content) > limit_bytes:
        return (
            b"",
            _error_result(
                error_code="ARTIFACT_OVERSIZED",
                message=f"redacted content length {len(content)} bytes exceeds limit {limit_bytes}",
                detail={
                    "limit_bytes": limit_bytes,
                    "actual_bytes": len(content),
                },
            ),
        )
    return (content, None)


def _redact_sensitive_text(
    value: str,
    settings: Settings,
    *,
    service_settings: ServiceSettings,
) -> str:
    """Redact known secrets and provider tokens from a text string."""
    redacted = value
    for secret in (settings.api_token, settings.github_token):
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "<redacted>")
    return provider_readiness_service.redact_launch_preflight_text(service_settings, redacted)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    """Wrap a JSON payload in a ``CallToolResult`` with text and structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )
