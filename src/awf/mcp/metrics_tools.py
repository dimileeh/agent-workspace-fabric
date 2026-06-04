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

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import AliasChoices, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.routes import metrics as metrics_routes
from awf.api.schemas import (
    WorkspaceLockListResponse,
    WorkspaceLockResponse,
    WorkspaceLogReadResponse,
    WorkspaceOverlapGraphResponse,
)
from awf.common.config import Settings, get_settings
from awf.common.redaction import REDACTION_MARKER, redact_secrets_byte_slice
from awf.common.token_patterns import compile_token_assignment_re
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.mcp.server import (
    _error_result,
    _null_tool_result,
    _provided_disk_check,
    _provided_health,
    _provided_local_capacity,
    _provided_orphan_resources,
    _provided_readiness,
    _provided_runtime_health,
    _tool_result,
)
from awf.service import config as service_config
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.disk import DiskCheck
from awf.service.locks import InvalidWorkspaceLockCursorError, list_workspace_lock_page_for_session
from awf.service.logs import _is_service_secret_env_key as _is_service_log_secret_env_key
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
from awf.service.provider_readiness import ProviderName
from awf.service.resource_capacity import LocalCapacityLimits
from awf.service.tasks import build_task_attempt_list_response, build_task_list_response
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary
from awf.service.workspaces import (
    WorkspaceService,
)

if TYPE_CHECKING:
    pass

StructuredToolResult = Annotated[CallToolResult, dict[str, Any]]


class SafeResult(Protocol):
    def __call__(self, payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult: ...


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
_LOG_REDACTION_CONTEXT_BYTES = 4096
_LOG_REDACTION_ASSIGNMENT_LOOKBACK_BYTES = 65_536
_LOG_REDACTION_VALUE_DELIMITER_BYTES = frozenset(b" \t\r\n\v\f\"'`,;)}]")
_UTF8_CONTINUATION_BYTE_MASK = 0b1100_0000
_UTF8_CONTINUATION_BYTE_VALUE = 0b1000_0000
_LOG_TOKEN_ASSIGNMENT_RE = compile_token_assignment_re()
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


def _workspace_log_redaction_secrets(
    settings: Settings,
    *,
    service_settings: service_config.ServiceSettings,
    compose_env_file: service_config.ComposeEnvFileInput = (
        service_config.COMPOSE_ENV_FILE_OMITTED
    ),
) -> tuple[str, ...]:
    """Return exact secret values that need context-aware log slice redaction."""
    secrets: list[str] = []
    for secret in (
        settings.api_token,
        settings.github_token,
        service_settings.api_token,
        service_settings.github_token,
    ):
        if secret and len(secret) >= 4:
            secrets.append(secret)
    provider_environ = _workspace_log_redaction_provider_environ(
        compose_env_file=compose_env_file,
    )
    for key, value in provider_environ.items():
        if _is_service_log_secret_env_key(key) and len(value) >= 4:
            secrets.append(value)
    return tuple(dict.fromkeys(secrets))


def _workspace_log_redaction_provider_environ(
    *,
    compose_file: Path | None = None,
    compose_env_file: service_config.ComposeEnvFileInput = service_config.COMPOSE_ENV_FILE_OMITTED,
) -> Mapping[str, str]:
    """Return local provider env values whose exact secrets must be redacted."""
    resolved_compose_env_file: service_config.ComposeEnvFileInput
    if isinstance(compose_env_file, service_config.ComposeEnvFileOmitted):
        resolved_compose_env_file = service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE
    else:
        resolved_compose_env_file = compose_env_file

    return service_config.resolve_local_service_provider_environ(
        provider_environ=None,
        environ=os.environ,
        compose_file=(
            compose_file if compose_file is not None else service_config.LOCAL_SERVICE_COMPOSE_FILE
        ),
        compose_env_file=resolved_compose_env_file,
    )


def _workspace_log_redaction_context_bytes(extra_secrets: tuple[str, ...]) -> int:
    """Return surrounding bytes to read before applying slice redaction."""
    return max(
        (len(secret.encode("utf-8")) + _LOG_REDACTION_CONTEXT_BYTES for secret in extra_secrets),
        default=_LOG_REDACTION_CONTEXT_BYTES,
    )


def _workspace_log_read_offset(*, requested_offset: int, redaction_context: int) -> int:
    """Return the expanded log read offset, retaining one byte to identify boundaries."""
    if requested_offset <= redaction_context:
        return 0
    return requested_offset - redaction_context - 1


def _leading_utf8_continuation_byte_count(data: bytes) -> int:
    """Return bytes to skip so decoding starts on a UTF-8 boundary."""
    for index, value in enumerate(data[:3]):
        if value & _UTF8_CONTINUATION_BYTE_MASK != _UTF8_CONTINUATION_BYTE_VALUE:
            return index
    return min(len(data), 3)


def _workspace_log_projection_text(
    result: dict[str, Any],
    *,
    result_offset: int,
) -> tuple[str, int]:
    """Return text and absolute byte offset for byte-window projection."""
    raw_bytes = result.get("raw_bytes")
    if not isinstance(raw_bytes, bytes):
        return str(result["text"]), result_offset

    boundary_shift = _leading_utf8_continuation_byte_count(raw_bytes) if result_offset > 0 else 0
    return raw_bytes[boundary_shift:].decode("utf-8", errors="surrogateescape"), (
        result_offset + boundary_shift
    )


def _unknown_leading_log_value_fragment_end(text: str, *, result_offset: int) -> int:
    """Return the byte end of a possibly mid-token leading fragment."""
    if result_offset <= 0 or not text:
        return 0

    characters = iter(text)
    first_bytes = next(characters).encode("utf-8", errors="surrogateescape")
    if not first_bytes or first_bytes[0] in _LOG_REDACTION_VALUE_DELIMITER_BYTES:
        return 0

    fragment_end = len(first_bytes)
    for char in characters:
        char_bytes = char.encode("utf-8", errors="surrogateescape")
        if char_bytes[0] in _LOG_REDACTION_VALUE_DELIMITER_BYTES:
            return fragment_end
        fragment_end += len(char_bytes)
    return fragment_end


def _workspace_log_slice_starts_in_unknown_leading_fragment(
    text: str,
    start: int,
    *,
    result_offset: int,
) -> bool:
    """Return whether the caller slice starts inside an unknown leading token."""
    fragment_end = _unknown_leading_log_value_fragment_end(text, result_offset=result_offset)
    return 0 <= start < fragment_end


def _workspace_log_assignment_value_covers_byte(text: str, start: int) -> bool:
    """Return whether visible assignment context already redacts ``start``."""
    if start < 0:
        return False
    for match in _LOG_TOKEN_ASSIGNMENT_RE.finditer(text):
        value_start = len(text[: match.start("value")].encode("utf-8", errors="surrogateescape"))
        if value_start > start:
            break
        value_end = value_start + len(
            match.group("value").encode("utf-8", errors="surrogateescape")
        )
        if value_start <= start < value_end:
            return True
    return False


async def _workspace_log_assignment_lookback_projection(
    service: WorkspaceService,
    workspace_id: str,
    stream_id: str,
    *,
    requested_offset: int,
    requested_limit_bytes: int,
    result: dict[str, Any],
    result_text: str,
    projection_offset: int,
) -> tuple[str, int, bool]:
    """Return a projection plus whether a failed lookback left a fragment untrusted."""
    slice_start = requested_offset - projection_offset
    if not _workspace_log_slice_starts_in_unknown_leading_fragment(
        result_text,
        slice_start,
        result_offset=projection_offset,
    ):
        return result_text, projection_offset, False
    if _workspace_log_assignment_value_covers_byte(result_text, slice_start):
        return result_text, projection_offset, False

    result_offset = int(result["offset"])
    if result_offset <= 0:
        return result_text, projection_offset, False

    lookback_offset = max(0, result_offset - _LOG_REDACTION_ASSIGNMENT_LOOKBACK_BYTES)
    read_limit = int(result["next_offset"]) - lookback_offset
    if read_limit <= 0:
        return result_text, projection_offset, True

    lookback_result = await service.read_log(
        workspace_id,
        stream_id,
        offset=lookback_offset,
        limit_bytes=read_limit,
        include_bytes=True,
    )
    required_next_offset = min(
        requested_offset + requested_limit_bytes,
        int(result["next_offset"]),
    )
    if lookback_result is None or int(lookback_result["next_offset"]) < required_next_offset:
        return result_text, projection_offset, True

    lookback_text, lookback_projection_offset = _workspace_log_projection_text(
        lookback_result,
        result_offset=int(lookback_result["offset"]),
    )
    lookback_slice_start = requested_offset - lookback_projection_offset
    still_unknown_fragment = _workspace_log_slice_starts_in_unknown_leading_fragment(
        lookback_text,
        lookback_slice_start,
        result_offset=lookback_projection_offset,
    )
    if still_unknown_fragment and not _workspace_log_assignment_value_covers_byte(
        lookback_text,
        lookback_slice_start,
    ):
        return lookback_text, lookback_projection_offset, True
    return lookback_text, lookback_projection_offset, False


def _redact_workspace_log_byte_slice(
    text: str,
    start: int,
    end: int,
    *,
    extra_secrets: tuple[str, ...],
    redact_unknown_leading_fragment: bool = False,
    result_offset: int = 0,
) -> str:
    """Redact a requested log byte slice using visible redaction context."""
    if redact_unknown_leading_fragment:
        fragment_end = _unknown_leading_log_value_fragment_end(text, result_offset=result_offset)
        if start < fragment_end:
            if end <= fragment_end:
                return REDACTION_MARKER
            return REDACTION_MARKER + redact_secrets_byte_slice(
                text,
                fragment_end,
                end,
                extra_secrets=extra_secrets,
            )
    return redact_secrets_byte_slice(text, start, end, extra_secrets=extra_secrets)


def _requested_log_window_offsets(
    *,
    requested_offset: int,
    limit_bytes: int,
    expanded_next_offset: int,
    expanded_eof: bool,
) -> tuple[int, bool]:
    """Project an expanded read result back to the caller's requested byte window."""
    if expanded_eof:
        file_size = expanded_next_offset
        safe_offset = min(requested_offset, file_size)
        next_offset = min(safe_offset + limit_bytes, file_size)
        return next_offset, next_offset >= file_size
    covered_next_offset = min(requested_offset + limit_bytes, expanded_next_offset)
    return max(requested_offset, covered_next_offset), False


def register_metrics_tools(
    mcp: FastMCP,
    service: WorkspaceService,
    safe_result: SafeResult,
    settings_value: Settings,
    disk_check_provider: DiskCheckProvider | None,
    local_capacity_provider: LocalCapacityProvider | None,
    orphan_resource_summary_provider: OrphanResourceSummaryProvider | None,
    runtime_health_summary_provider: RuntimeHealthSummaryProvider | None,
    readiness_provider: ReadinessProvider | None,
    health_provider: HealthProvider | None,
    compose_env_file: service_config.ComposeEnvFileInput = service_config.COMPOSE_ENV_FILE_OMITTED,
) -> None:
    _safe_result = safe_result

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
        service_settings = service_config.resolve_service_settings(settings_value)
        extra_secrets = _workspace_log_redaction_secrets(
            settings_value,
            service_settings=service_settings,
            compose_env_file=compose_env_file,
        )
        redaction_context = _workspace_log_redaction_context_bytes(extra_secrets)
        read_offset = _workspace_log_read_offset(
            requested_offset=offset,
            redaction_context=redaction_context,
        )
        read_limit = offset - read_offset + limit_bytes + redaction_context
        result = await service.read_log(
            workspace_id,
            stream_id,
            offset=read_offset,
            limit_bytes=read_limit,
            include_bytes=True,
        )
        if result is None:
            return None
        result_offset = int(result["offset"])
        result_next_offset = int(result["next_offset"])
        requested_next_offset, requested_eof = _requested_log_window_offsets(
            requested_offset=offset,
            limit_bytes=limit_bytes,
            expanded_next_offset=result_next_offset,
            expanded_eof=bool(result["eof"]),
        )
        result_text, projection_offset = _workspace_log_projection_text(
            result,
            result_offset=result_offset,
        )
        (
            result_text,
            projection_offset,
            redact_unknown_leading_fragment,
        ) = await _workspace_log_assignment_lookback_projection(
            service,
            workspace_id,
            stream_id,
            requested_offset=offset,
            requested_limit_bytes=limit_bytes,
            result=result,
            result_text=result_text,
            projection_offset=projection_offset,
        )
        data = _redact_workspace_log_byte_slice(
            result_text,
            offset - projection_offset,
            offset - projection_offset + limit_bytes,
            extra_secrets=extra_secrets,
            redact_unknown_leading_fragment=redact_unknown_leading_fragment,
            result_offset=projection_offset,
        )
        return WorkspaceLogReadResponse(
            stream_id=str(result["stream_id"]),
            offset=offset,
            next_offset=requested_next_offset,
            eof=requested_eof,
            data=data,
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
            description=(
                "Optional list of provider names to restrict readiness checks to "
                "(e.g. 'github', 'codex', 'claude_code', 'cursor', "
                "'gemini', 'opencode', 'grok', 'docker'). When set, "
                "only these providers affect the overall readiness outcome."
            ),
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
