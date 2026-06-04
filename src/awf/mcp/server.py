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
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import AliasChoices, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    ErrorResponse,
    WorkspaceAcceptedResponse,
)
from awf.common.config import Settings, get_settings
from awf.common.redaction import REDACTION_MARKER, redact_secrets
from awf.db.repositories import TaskExternalIdConflictError
from awf.service import config as service_config
from awf.service import provider_readiness as provider_readiness_service
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.local_capacity import detect_local_capacity
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.provider_readiness import ProviderName, is_secret_env_key
from awf.service.resource_capacity import LocalCapacityLimits
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary
from awf.service.workspaces import (
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


# ── MCP tool registration ─────────────────────────────────────────────────


def build_mcp_server(
    *,
    service: WorkspaceService,
    name: str = "awf",
    instructions: str | None = None,
    settings: Settings | None = None,
    compose_env_file: service_config.ComposeEnvFileInput = service_config.COMPOSE_ENV_FILE_OMITTED,
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
    service_settings_value = service_config.resolve_service_settings(settings_value)
    extra_secret_values = _mcp_secret_values(
        settings_value,
        service_settings_value,
        compose_env_file=compose_env_file,
    )

    def _safe_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
        """Redact sensitive data from *payload* and wrap in a ``CallToolResult``."""
        redacted = _redact_sensitive_payload(
            payload,
            settings_value,
            service_settings=service_settings_value,
            extra_secrets=extra_secret_values,
        )
        return _tool_result(redacted, is_error=is_error)

    from awf.mcp.control_tools import register_control_tools
    from awf.mcp.metrics_tools import register_metrics_tools
    from awf.mcp.workspace_tools import register_workspace_tools

    register_workspace_tools(
        mcp=mcp,
        service=service,
        safe_result=_safe_result,
        settings_value=settings_value,
        disk_check_provider=disk_check_provider,
        extra_secrets=extra_secret_values,
    )
    register_metrics_tools(
        mcp=mcp,
        service=service,
        safe_result=_safe_result,
        settings_value=settings_value,
        disk_check_provider=disk_check_provider,
        local_capacity_provider=local_capacity_provider,
        orphan_resource_summary_provider=orphan_resource_summary_provider,
        runtime_health_summary_provider=runtime_health_summary_provider,
        readiness_provider=readiness_provider,
        health_provider=health_provider,
        compose_env_file=compose_env_file,
        service_settings=service_settings_value,
        extra_secrets=extra_secret_values,
    )
    register_control_tools(
        mcp=mcp,
        service=service,
        safe_result=_safe_result,
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
        _check_worker_heartbeat,
        _workspace_view_for_readyz,
    )
    from awf.common.commands import AsyncioSubprocessRunner
    from awf.service.config import resolve_service_settings
    from awf.service.node_identity import effective_service_node_id
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
    service_settings = resolve_service_settings(settings)
    worker_node_id = effective_service_node_id(service_settings)
    runner = AsyncioSubprocessRunner()
    db_check_task: asyncio.Task[CheckResult] = asyncio.create_task(_check_db(session_factory))
    worker_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_worker_heartbeat(session_factory, node_id=worker_node_id)
    )
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
            min_retention_hours=service_settings.completed_workspace_retention_hours,
        )
    )
    docker_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        scan_docker_resources_async(
            runner=runner,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    )
    worktree_scan_task: asyncio.Task[ResourceScan] = asyncio.create_task(
        asyncio.to_thread(scan_managed_worktrees, service_settings.work_dir)
    )
    orphan_check_task: asyncio.Task[CheckResult] = asyncio.create_task(
        _check_orphan_resources_with_concurrent_scans(
            db_check_task=db_check_task,
            docker_check_task=daemon_check_task,
            workspace_view_task=workspace_view_task,
            docker_scan_task=docker_scan_task,
            worktree_scan_task=worktree_scan_task,
            auto_cleanup_orphans=service_settings.auto_cleanup_orphans,
        )
    )
    agent_readiness_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
        asyncio.to_thread(
            collect_agent_readiness,
            service_settings,
            **readiness_kwargs,
        )
    )
    await asyncio.gather(
        db_check_task,
        worker_check_task,
        cli_check_task,
        daemon_check_task,
        compose_check_task,
        image_check_task,
        agent_readiness_task,
        orphan_check_task,
    )
    db_check = db_check_task.result()
    worker_check = worker_check_task.result()
    cli_check = cli_check_task.result()
    daemon_check = daemon_check_task.result()
    compose_check = compose_check_task.result()
    image_check = image_check_task.result()
    agent_readiness = agent_readiness_task.result()
    orphan_check = orphan_check_task.result()
    checks = {
        "db": db_check,
        "worker": worker_check,
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


def _mcp_secret_values(
    settings: Settings,
    service_settings: ServiceSettings,
    *,
    compose_env_file: service_config.ComposeEnvFileInput = service_config.COMPOSE_ENV_FILE_OMITTED,
) -> tuple[str, ...]:
    """Return exact secret values that MCP payloads and artifacts must redact."""
    resolved_compose_env_file: service_config.ComposeEnvFileInput
    if isinstance(compose_env_file, service_config.ComposeEnvFileOmitted):
        resolved_compose_env_file = service_config.LOCAL_SERVICE_COMPOSE_ENV_FILE
    else:
        resolved_compose_env_file = compose_env_file
    provider_environ = service_config.resolve_local_service_provider_environ(
        provider_environ=None,
        environ=os.environ,
        compose_file=service_config.LOCAL_SERVICE_COMPOSE_FILE,
        compose_env_file=resolved_compose_env_file,
    )
    values: list[str | None] = [
        settings.api_token,
        settings.github_token,
        service_settings.api_token,
        service_settings.github_token,
    ]
    values.extend(value for key, value in provider_environ.items() if is_secret_env_key(key))
    return tuple(dict.fromkeys(value for value in values if value and len(value) >= 4))


def _redact_sensitive_payload(
    payload: dict[str, Any],
    settings: Settings,
    *,
    service_settings: ServiceSettings | None = None,
    extra_secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """Redact secrets from a JSON payload dict using application settings."""
    resolved_service_settings = service_settings or service_config.resolve_service_settings(
        settings
    )
    redacted = _redact_sensitive_value(
        payload,
        settings,
        service_settings=resolved_service_settings,
        extra_secrets=tuple(extra_secrets),
    )
    return redacted if isinstance(redacted, dict) else {}


def _redact_sensitive_value(
    value: Any,
    settings: Settings,
    *,
    service_settings: ServiceSettings,
    extra_secrets: Iterable[str] = (),
) -> Any:
    """Recursively redact secrets from an arbitrary value (dict, list, or string)."""
    extra_secret_values = tuple(extra_secrets)
    if isinstance(value, str):
        return _redact_sensitive_text(
            value,
            settings,
            service_settings=service_settings,
            extra_secrets=extra_secret_values,
        )
    if isinstance(value, list):
        return [
            _redact_sensitive_value(
                item,
                settings,
                service_settings=service_settings,
                extra_secrets=extra_secret_values,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            _redact_sensitive_text(
                key,
                settings,
                service_settings=service_settings,
                extra_secrets=extra_secret_values,
            )
            if isinstance(key, str)
            else key: _redact_sensitive_value(
                item,
                settings,
                service_settings=service_settings,
                extra_secrets=extra_secret_values,
            )
            for key, item in value.items()
        }
    return value


def _contains_secret_bytes(
    content: bytes,
    settings: Settings,
    *,
    service_settings: ServiceSettings,
    extra_secrets: Iterable[str] = (),
) -> bool:
    """Check whether binary content contains configured secrets or recognizable token patterns."""
    extra_secret_values = tuple(extra_secrets)
    for secret in (
        settings.api_token,
        settings.github_token,
        service_settings.github_token,
        *extra_secret_values,
    ):
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
    if redact_secrets(decoded, extra_secrets=extra_secret_values) != decoded:
        return True
    # Retain service preflight patterns for edge cases the shared redaction
    # patterns intentionally do not cover, such as tokens adjacent to "_" and
    # URL credentials without a word-boundary prefix.
    if provider_readiness_service.TOKEN_RE.search(decoded) is not None:
        return True
    # Additionally block URL credentials that the text path would redact.
    return provider_readiness_service.URL_CREDENTIAL_RE.search(decoded) is not None


def _redact_exact_secret_bytes(
    content: bytes,
    settings: Settings,
    service_settings: ServiceSettings,
    extra_secrets: Iterable[str],
) -> bytes:
    """Redact exact configured secret byte sequences before text decoding."""
    secrets = {
        secret.encode("utf-8")
        for secret in (
            settings.api_token,
            settings.github_token,
            service_settings.github_token,
            *extra_secrets,
        )
        if secret and len(secret) >= 4
    }
    spans: list[tuple[int, int]] = []
    for secret in secrets:
        cursor = 0
        while True:
            start = content.find(secret, cursor)
            if start == -1:
                break
            spans.append((start, start + len(secret)))
            cursor = start + 1
    if not spans:
        return content

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    marker = REDACTION_MARKER.encode("ascii")
    pieces: list[bytes] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            pieces.append(content[cursor:start])
        pieces.append(marker)
        cursor = end
    if cursor < len(content):
        pieces.append(content[cursor:])
    return b"".join(pieces)


def _check_and_redact_artifact_content(
    content: bytes,
    limit_bytes: int,
    settings: Settings,
    service_settings: ServiceSettings,
    is_likely_text: bool,
    extra_secrets: Iterable[str] = (),
) -> tuple[bytes, CallToolResult | None]:
    """Apply BOM blocking, text redaction, binary secret detection, and size recheck.

    Runs synchronously so it can be off-loaded to ``asyncio.to_thread``.
    Returns ``(content, error_result)`` where ``error_result`` is non-None when
    the artifact must be blocked or is oversized after redaction.
    """
    extra_secret_values = tuple(extra_secrets)
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
        content = _redact_exact_secret_bytes(
            content,
            settings,
            service_settings,
            extra_secret_values,
        )
        text = content.decode("latin-1")
        redacted_text = _redact_sensitive_text(
            text,
            settings,
            service_settings=service_settings,
            extra_secrets=extra_secret_values,
        )
        content = redacted_text.encode("latin-1")
    elif _contains_secret_bytes(
        content,
        settings,
        service_settings=service_settings,
        extra_secrets=extra_secret_values,
    ):
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
    extra_secrets: Iterable[str] = (),
) -> str:
    """Redact known secrets and provider tokens from a text string."""
    extra_secret_values = tuple(extra_secrets)
    redacted = value
    for secret in (settings.api_token, settings.github_token):
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = provider_readiness_service.redact_launch_preflight_text(service_settings, redacted)
    return redact_secrets(redacted, extra_secrets=extra_secret_values)


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    """Wrap a JSON payload in a ``CallToolResult`` with text and structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )
