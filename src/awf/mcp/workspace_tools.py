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
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import AliasChoices, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    ErrorResponse,
    OwnedPath,
    WorkspaceArtifactReadResponse,
    WorkspaceCreateRequest,
    WorkspaceEventListResponse,
    WorkspaceLogListResponse,
)
from awf.common.config import Settings, get_settings
from awf.common.workspace_policy import DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    TaskKind,
    WorkspaceStatus,
)
from awf.db.repositories import TaskExternalIdConflictError, WorkspaceRepository
from awf.mcp.server import (
    _check_and_redact_artifact_content,
    _error_result,
    _idempotency_key_error,
    _normalize_mcp_idempotency_key,
    _null_tool_result,
    _provider_readiness_blocked_result,
    _redact_sensitive_payload,
    _redact_sensitive_text,
    _required_idempotency_key,
    _task_external_id_conflict_result,
    _tool_error,
    _tool_result,
    _workspace_accepted_payload,
    _workspace_admission_disk_check,
    _workspace_error_result,
    _workspace_retry_error_result,
)
from awf.profiles.resolver import ProfileResolutionError
from awf.service import config as service_config
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
from awf.service.disk import DiskCheck
from awf.service.merge_queue import InvalidMergeQueueCursorError, list_merge_queue_response
from awf.service.operations import build_operation_list_response
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.provider_readiness import ProviderName
from awf.service.resource_capacity import LocalCapacityLimits
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
    WorkspaceCreateDuplicateHostPortError,
    WorkspaceCreateHostPortConflictError,
    WorkspaceCreateIdempotencyConflictError,
    WorkspaceCreateInsufficientDiskError,
    WorkspaceProviderReadinessBlockedError,
    WorkspaceRetryError,
    WorkspaceRetrySourceRuntimeNotReleasedError,
    WorkspaceService,
)

if TYPE_CHECKING:
    pass

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]


class SafeResult(Protocol):
    """Protocol for constructing safe MCP tool result objects."""

    def __call__(self, payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
        """Build a safe MCP tool result from a payload dict."""
        ...


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
_MCP_LEGACY_BASE_BRANCH_DEFAULT = "development"
# sync_release_pr omits base_branch -> target the release branch (main), not the
# legacy development default, so the release PR is opened development -> main
# instead of degenerating to development -> development (NO_CHANGES_TO_SYNC).
_MCP_RELEASE_SYNC_BASE_BRANCH_DEFAULT = "main"


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


def register_workspace_tools(
    mcp: FastMCP,
    service: WorkspaceService,
    safe_result: SafeResult,
    settings_value: Settings,
    disk_check_provider: DiskCheckProvider | None,
    extra_secrets: Iterable[str] = (),
) -> None:
    """Register all AWF workspace MCP tools on the given FastMCP server."""

    _safe_result = safe_result
    extra_secret_values = tuple(extra_secrets)

    @mcp.tool(name="awf_create_workspace")
    async def awf_create_workspace(
        repo_url: str = Field(..., description="Git URL the workspace should check out."),
        base_branch: str | None = Field(
            default=None,
            min_length=1,
            max_length=256,
            json_schema_extra={"default": _MCP_LEGACY_BASE_BRANCH_DEFAULT},
            description=(
                "Target branch: feature_branch_pr branches FROM it; "
                "sync_release_pr opens the release PR against it. Defaults to "
                f"{_MCP_LEGACY_BASE_BRANCH_DEFAULT} for feature_branch_pr and "
                f"{_MCP_RELEASE_SYNC_BASE_BRANCH_DEFAULT} for sync_release_pr when omitted."
            ),
        ),
        branch_base: str | None = Field(
            default=None,
            min_length=1,
            max_length=256,
            description="Legacy alias for base_branch.",
        ),
        source_branch: str | None = Field(
            default=None,
            min_length=1,
            max_length=256,
            json_schema_extra={"default": DEFAULT_RELEASE_SYNC_SOURCE_BRANCH},
            description=(
                "Source branch for sync_release_pr: the release PR is opened "
                f"source_branch -> base_branch. Defaults to "
                f"{DEFAULT_RELEASE_SYNC_SOURCE_BRANCH} when omitted. Ignored for "
                "feature_branch_pr."
            ),
        ),
        task_title: str = Field(..., description="Short title of the task (≤ 512 chars)."),
        task_prompt: str = Field(..., description="Full prompt to hand to the coding CLI."),
        task_kind: str = Field(
            default="feature_branch_pr",
            description=(
                "Task kind: 'feature_branch_pr' (default) or 'sync_release_pr' "
                "(open/reuse a source->target release PR, monitored without "
                "auto-merge). The deprecated 'monitor_release_pr' and the "
                "adoption-only 'sync_feature_pr' are rejected here; to monitor an "
                "existing PR, adopt it with auto_merge=false."
            ),
        ),
        agent: AgentRuntime = Field(
            default=AgentRuntime.codex,
            description="Which coding CLI to run inside the container.",
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
        task_external_id: str | None = Field(
            default=None, description="Optional caller-side task ID for correlation."
        ),
        task_tag: str | None = Field(
            default=None,
            max_length=64,
            description=(
                "Optional Jira issue key (e.g. PROJ-123) prepended to the branch, PR "
                "title, and every AWF-authored commit message so work links to the issue."
            ),
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
        env_profile: str | None = Field(
            default=None,
            max_length=128,
            description="Legacy alias for profile_ref.",
        ),
        profile: dict[str, Any] | None = Field(
            default=None,
            description="Optional inline workspace profile dictionary.",
        ),
        validation_commands: list[str] | None = Field(
            default=None,
            json_schema_extra={"default": []},
            description="Shell commands to validate the change.",
        ),
        test_commands: list[str] | None = Field(
            default=None,
            description="Legacy alias for validation_commands.",
        ),
        requires_database: bool = Field(
            default=False,
            description="Legacy database-profile shortcut; maps to profile_ref='aira'.",
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
        companions: list[dict[str, Any]] | None = Field(
            default=None,
            max_length=16,
            description=(
                "Optional managed companion services. Each item is a companion "
                "object with repo_url, name, and repo-relative service paths."
            ),
        ),
        idempotency_key: str | None = Field(
            default=None,
            description="Optional replay key matching the REST Idempotency-Key header.",
        ),
    ) -> StructuredToolResult:
        """Create a new AWF workspace using the canonical rich v1 contract."""
        if branch_base is not None and base_branch is not None and branch_base != base_branch:
            return _error_result(
                "INVALID_REQUEST",
                "Provide either base_branch or branch_base, or ensure they match.",
            )
        if (
            test_commands is not None
            and validation_commands is not None
            and test_commands != validation_commands
        ):
            return _error_result(
                "INVALID_REQUEST",
                "Provide either validation_commands or test_commands, or ensure they match.",
            )
        if (
            not requires_database
            and env_profile is not None
            and profile_ref not in (None, "auto", env_profile)
        ):
            return _error_result(
                "INVALID_REQUEST",
                "Provide either profile_ref or env_profile, or ensure they match.",
            )
        if requires_database and (
            profile_ref not in (None, "auto", "aira") or env_profile not in (None, "aira")
        ):
            return _error_result(
                "INVALID_REQUEST",
                "Provide either requires_database or profile_ref/env_profile='aira'.",
            )

        default_base_branch = (
            _MCP_RELEASE_SYNC_BASE_BRANCH_DEFAULT
            if task_kind == TaskKind.sync_release_pr.value
            else _MCP_LEGACY_BASE_BRANCH_DEFAULT
        )
        effective_base_branch = branch_base or base_branch or default_base_branch
        effective_validation_commands = (
            test_commands if test_commands is not None else validation_commands or []
        )
        effective_profile_ref = "aira" if requires_database else env_profile or profile_ref
        req = WorkspaceCreateRequest(
            repo={
                "url": repo_url,
                "base_branch": effective_base_branch,
                "source_branch": source_branch,
            },
            task={
                k: v
                for k, v in {
                    "title": task_title,
                    "prompt": task_prompt,
                    "kind": task_kind,
                    "agent": agent,
                    "model": model,
                    "effort": effort,
                    "external_id": task_external_id,
                    "task_tag": task_tag,
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
            workspace={"profile_ref": effective_profile_ref, "profile": profile},
            validation={
                "commands": effective_validation_commands,
                "requested_tier": requested_tier,
            },
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
            companions=companions or [],
        )

        async def resolve_disk_check() -> DiskCheck:
            """Resolve the disk-check provider for workspace admission gating."""
            return await _workspace_admission_disk_check(
                disk_check_provider=disk_check_provider,
                settings=settings_value,
            )

        try:
            ws = await service.create(
                req,
                idempotency_key=_normalize_mcp_idempotency_key(idempotency_key),
                disk_check_factory=resolve_disk_check,
            )
        except WorkspaceCreateIdempotencyConflictError as exc:
            return _workspace_error_result(exc)
        except WorkspaceCreateInsufficientDiskError as exc:
            return _workspace_error_result(exc)
        except WorkspaceCreateHostPortConflictError as exc:
            return _workspace_error_result(exc)
        except WorkspaceCreateDuplicateHostPortError as exc:
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
        except WorkspaceCreateHostPortConflictError as exc:
            return _workspace_error_result(exc)
        except WorkspaceCreateDuplicateHostPortError as exc:
            return _workspace_error_result(exc)
        except WorkspaceRetrySourceRuntimeNotReleasedError as exc:
            return _workspace_error_result(exc)
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
                    str(exc),
                    settings_value,
                    service_settings=_service_settings,
                    extra_secrets=extra_secret_values,
                ),
            )
        except ArtifactNotFoundError:
            return _error_result(
                "NOT_FOUND",
                _redact_sensitive_text(
                    f"No artifact at path {relative_path}",
                    settings_value,
                    service_settings=_service_settings,
                    extra_secrets=extra_secret_values,
                ),
            )
        except ArtifactOversizedError as exc:
            return _error_result(
                error_code="ARTIFACT_OVERSIZED",
                message=_redact_sensitive_text(
                    str(exc),
                    settings_value,
                    service_settings=_service_settings,
                    extra_secrets=extra_secret_values,
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
            extra_secret_values,
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
        redacted_payload = _redact_sensitive_payload(
            payload,
            settings_value,
            service_settings=_service_settings,
            extra_secrets=extra_secret_values,
        )
        redacted_payload["content"] = encoded_content
        return _tool_result(redacted_payload)
