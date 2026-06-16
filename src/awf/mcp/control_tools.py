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

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import AliasChoices, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import (
    OperationResponse,
    OwnedPath,
    PullRequestMonitorAdoptionRequest,
)
from awf.common.audit import redact_audit_text
from awf.common.config import Settings, get_settings
from awf.db.enums import (
    AgentRuntime,
)
from awf.mcp.server import (
    _idempotency_key_error,
    _required_idempotency_key,
    _tool_error,
    _tool_result,
    _workspace_error_result,
)
from awf.service.controls import WorkspaceControlError
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.pr_monitor_adoption import PRMonitorAdoptionError
from awf.service.provider_readiness import ProviderName
from awf.service.resource_capacity import LocalCapacityLimits
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


def register_control_tools(
    mcp: FastMCP,
    service: WorkspaceService,
    safe_result: SafeResult,
) -> None:
    _safe_result = safe_result

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
        owned_paths: list[OwnedPath] = Field(
            default_factory=list,
            max_length=128,
            description=(
                "Operator-approved owned paths for the adopted monitor, including "
                "protected files when review or CI repair is expected to touch them."
            ),
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
        task_tag: str | None = Field(
            default=None,
            max_length=64,
            description=(
                "Optional Jira issue key (e.g. PROJ-123) prepended to every "
                "AWF-authored monitor commit message so fixes link to the issue."
            ),
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
                    owned_paths=owned_paths,
                    auto_merge=auto_merge,
                    initial_review_grace_period_seconds=(initial_review_grace_period_seconds),
                    task_title=task_title,
                    task_prompt=task_prompt,
                    task_tag=task_tag,
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

    @mcp.tool(name="awf_guide_workspace")
    async def awf_guide_workspace(
        workspace_id: str = Field(
            ..., min_length=1, max_length=256, description="Workspace ID to guide."
        ),
        directive: str = Field(
            default="",
            max_length=1024,
            description=(
                "Operator instruction for the agent's next monitor cycle "
                "(acted on, not deferred); distinct from the audit reason. Optional "
                "for a blocked workspace resolved with grants alone."
            ),
        ),
        reason: str | None = Field(
            default=None,
            max_length=1024,
            description="Optional operator audit reason (required when granting paths).",
        ),
        grants: list[str] | None = Field(
            default=None,
            description=(
                "For a blocked workspace: scoped, repo-relative path globs to "
                "APPROVE-AND-KEEP. At least one of directive/grants is required."
            ),
        ),
        approve_policy_downgrade: bool = Field(
            default=False,
            description="Acknowledge that a granted edit may weaken validation.",
        ),
        operator: str | None = Field(
            default=None,
            max_length=256,
            description="Operator identity recorded on grant audit records.",
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
        """Operator control: inject a directive and/or scoped grants into a workspace; not shell access."""
        idempotency_key_value = _required_idempotency_key(idempotency_key)
        if idempotency_key_value is None:
            return _idempotency_key_error()
        try:
            result = await service.guide_workspace(
                workspace_id,
                directive=directive,
                reason=reason,
                grants=grants,
                approve_policy_downgrade=approve_policy_downgrade,
                operator=operator,
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
