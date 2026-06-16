"""Pydantic schemas for the public API.

These schemas define the HTTP contract and are reused by the MCP server so
REST + MCP stay in lockstep. Keep them narrow: business objects live in
``awf.db.models``, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from awf.api import schemas_operations as _schemas_operations
from awf.api import schemas_responses as _schemas_responses
from awf.api.schemas_companions import WorkspaceCompanionRequest
from awf.common.task_tag import validate_task_tag
from awf.db.enums import (
    DEPRECATED_MONITOR_RELEASE_PR_TASK_KIND,
    AgentRuntime,
    TaskClass,
    TaskKind,
    WorkspaceStatus,
)
from awf.profiles.models import OutOfScopeChangePolicy, WorkspaceProfile

OwnedPath = _schemas_operations.OwnedPath
ValidationCommand = _schemas_operations.ValidationCommand
MergeBlockerReason = _schemas_operations.MergeBlockerReason
MergeCandidateStatus = _schemas_operations.MergeCandidateStatus
MergeQueueBlockerState = _schemas_operations.MergeQueueBlockerState
ValidationTier = _schemas_operations.ValidationTier
ValidationProvenanceStatus = _schemas_operations.ValidationProvenanceStatus
ValidationFreshnessStatus = _schemas_operations.ValidationFreshnessStatus
ValidationIdentitySource = _schemas_operations.ValidationIdentitySource
AgentIdentitySource = _schemas_operations.AgentIdentitySource
WorkspaceLifecycleStageStatus = _schemas_operations.WorkspaceLifecycleStageStatus
LlmUsageStatus = _schemas_operations.LlmUsageStatus
WorkspaceOverlapGraphQueueState = _schemas_operations.WorkspaceOverlapGraphQueueState
WorkspaceOverlapGraphSeverity = _schemas_operations.WorkspaceOverlapGraphSeverity
WorkspaceOverlapGraphReasonCode = _schemas_operations.WorkspaceOverlapGraphReasonCode
WorkspaceOverlapPathMatchReasonCode = _schemas_operations.WorkspaceOverlapPathMatchReasonCode
CallbackEventType = _schemas_operations.CallbackEventType
NetworkPosture = _schemas_operations.NetworkPosture
OperationListResponse = _schemas_operations.OperationListResponse
OperationResponse = _schemas_operations.OperationResponse
log_stream_ids = _schemas_operations.log_stream_ids
merge_log_stream_ref_value = _schemas_operations.merge_log_stream_ref_value

# Leaf request/response schemas live in ``awf.api.schemas_responses`` to keep
# this module under the maintainability line limit. Re-exported here so
# ``from awf.api.schemas import X`` keeps working for the REST app and the MCP
# server (definitions are relocated, not changed).
CallbackSubscriptionCreateRequest = _schemas_responses.CallbackSubscriptionCreateRequest
CallbackSubscriptionResponse = _schemas_responses.CallbackSubscriptionResponse
CallbackSubscriptionListResponse = _schemas_responses.CallbackSubscriptionListResponse
WorkspaceReasonRequest = _schemas_responses.WorkspaceReasonRequest
_WorkspaceReasonCompatibilityRequest = _schemas_responses._WorkspaceReasonCompatibilityRequest
WorkspaceReasonWithLegacyStopStackRequest = (
    _schemas_responses.WorkspaceReasonWithLegacyStopStackRequest
)
WorkspaceReasonWithLegacyRequestedTierRequest = (
    _schemas_responses.WorkspaceReasonWithLegacyRequestedTierRequest
)
WorkspaceControlRequest = _schemas_responses.WorkspaceControlRequest
WorkspaceGuideRequest = _schemas_responses.WorkspaceGuideRequest
WorkspaceOperationRequest = _schemas_responses.WorkspaceOperationRequest
WorkspaceControlWarningResponse = _schemas_responses.WorkspaceControlWarningResponse
WorkspaceControlResponse = _schemas_responses.WorkspaceControlResponse
ErrorResponse = _schemas_responses.ErrorResponse
HTTPExceptionErrorResponse = _schemas_responses.HTTPExceptionErrorResponse
HttpExceptionErrorResponse = _schemas_responses.HttpExceptionErrorResponse
GCTerminalStatus = _schemas_responses.GCTerminalStatus
ServiceGCRequest = _schemas_responses.ServiceGCRequest
ServiceGCResponse = _schemas_responses.ServiceGCResponse
ServiceGCWorkerReclaim = _schemas_responses.ServiceGCWorkerReclaim

_MAX_LOG_STREAM_REF_DEPTH = 64
_DEFAULT_REPO_BASE_BRANCH = "main"
_LEGACY_FLAT_REPO_BASE_BRANCH_DEFAULT = "development"
_LEGACY_DATABASE_PROFILE_REF = "aira"
PUBLIC_DIRECT_CREATE_TASK_KINDS = _schemas_operations.PUBLIC_DIRECT_CREATE_TASK_KINDS


class MergeCandidateReadinessResponse(BaseModel):
    ready: bool
    manual_merge_required: bool
    waiting_for_monitor: bool
    failed_or_cancelled: bool
    completed: bool
    not_canonical: bool
    stale: bool
    stale_reason: str | None = None


class WorkspaceRepo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: Annotated[str, Field(min_length=1, max_length=512)]
    base_branch: Annotated[
        str, Field(default=_DEFAULT_REPO_BASE_BRANCH, min_length=1, max_length=256)
    ]
    source_branch: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    """Optional source branch for ``sync_release_pr`` (defaults to ``development``).
    The release PR is opened ``source_branch`` → ``base_branch``."""


class WorkspaceProviderFallbackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent: AgentRuntime
    provider: Annotated[str | None, Field(default=None, min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=128)]


class WorkspaceProviderRecoveryCircuitBreakerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=2, ge=1, le=100)
    cooldown_seconds: int = Field(default=900, ge=1, le=86400)


class WorkspaceProviderRecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallbacks: list[WorkspaceProviderFallbackTarget] = Field(
        default_factory=list,
        max_length=16,
    )
    max_fallback_attempts: int | None = Field(default=None, ge=0, le=16)
    max_same_provider_retries: int = Field(default=1, ge=0, le=16)
    cooldown_seconds: int = Field(default=300, ge=1, le=86400)
    backoff_seconds: int | None = Field(default=None, ge=1, le=86400)
    retry_after_cap_seconds: int = Field(default=3600, ge=1, le=86400)
    circuit_breaker: WorkspaceProviderRecoveryCircuitBreakerPolicy | None = None


class WorkspaceLaunchPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_readiness_override: bool = False
    provider_readiness_override_reason: Annotated[
        str | None, Field(default=None, max_length=512)
    ] = None


class WorkspaceTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Annotated[str, Field(min_length=1, max_length=512)]
    prompt: Annotated[str, Field(min_length=1, max_length=16384)]
    kind: Annotated[str, Field(default="feature_branch_pr", max_length=32)]
    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    model: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None
    effort: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None
    external_id: Annotated[str | None, Field(default=None, max_length=128)]
    task_tag: Annotated[str | None, Field(default=None, max_length=64)] = None
    task_class: TaskClass | None = None
    priority: int = Field(default=0, ge=0, le=100)
    human_boost: int = Field(default=0, ge=0, le=5)
    owned_paths: list[OwnedPath] = Field(default_factory=list, max_length=128)
    out_of_scope_changes: OutOfScopeChangePolicy | None = None
    auto_merge: bool = True
    initial_review_grace_period_seconds: float | None = Field(
        default=None,
        ge=0,
        le=86400,
    )
    provider_recovery: WorkspaceProviderRecoveryPolicy | None = None

    @field_validator("task_tag")
    @classmethod
    def _validate_task_tag(cls, value: str | None) -> str | None:
        """Normalize + validate the optional Jira issue key; ``None`` when absent."""
        return validate_task_tag(value)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        """Admit only the public direct-create task kinds.

        Rejects the deprecated ``monitor_release_pr``, the adoption-only
        ``sync_feature_pr``, and any unknown string so unsupported kinds can
        never reach feature provisioning/execution. Covers REST and MCP, which
        both funnel through this schema.
        """
        if value in PUBLIC_DIRECT_CREATE_TASK_KINDS:
            return value
        if value == DEPRECATED_MONITOR_RELEASE_PR_TASK_KIND:
            raise ValueError(
                "task kind 'monitor_release_pr' is deprecated; monitor an existing "
                "release/manual PR via PR adoption with auto_merge=false instead."
            )
        if value == TaskKind.sync_feature_pr.value:
            raise ValueError(
                "task kind 'sync_feature_pr' is created through the PR-adoption "
                "endpoint, not direct workspace creation."
            )
        supported = ", ".join(sorted(PUBLIC_DIRECT_CREATE_TASK_KINDS))
        raise ValueError(f"unsupported task kind {value!r}; supported kinds are: {supported}.")


class WorkspaceProfileSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_ref: Annotated[str | None, Field(default="auto", max_length=128)]
    profile: WorkspaceProfile | None = None


class WorkspaceValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    commands: list[ValidationCommand] = Field(default_factory=list)
    requested_tier: int = Field(default=1, ge=1, le=3)


class WorkspaceResources(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cpu: float | None = Field(default=None, gt=0)
    memory: Annotated[str | None, Field(default=None, max_length=32)]
    steady_state_cpu_cores: float | None = Field(default=None, gt=0)
    steady_state_memory_gb: float | None = Field(default=None, gt=0)
    peak_cpu_cores: float | None = Field(default=None, gt=0)
    peak_memory_gb: float | None = Field(default=None, gt=0)
    disk_mb: int | None = Field(default=None, gt=0)


class WorkspaceCreateRequest(BaseModel):
    """Canonical workspace creation contract for ``POST /v1/workspaces``."""

    model_config = ConfigDict(extra="forbid")

    repo: WorkspaceRepo
    task: WorkspaceTask
    workspace: WorkspaceProfileSelection = Field(
        default_factory=lambda: WorkspaceProfileSelection(profile_ref="auto", profile=None)
    )
    validation: WorkspaceValidation = Field(default_factory=lambda: WorkspaceValidation())
    resources: WorkspaceResources = Field(
        default_factory=lambda: WorkspaceResources(cpu=None, memory=None)
    )
    preflight: WorkspaceLaunchPreflight = Field(default_factory=lambda: WorkspaceLaunchPreflight())
    companions: list[WorkspaceCompanionRequest] = Field(default_factory=list, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_flat_payload(cls, data: object) -> object:
        """Accept old local flat payloads while exposing one rich public schema.

        AWF is pre-stable, so docs and OpenAPI advertise only the rich v1
        contract. This compatibility adapter keeps older tests and local callers
        from failing abruptly during the cleanup window.
        """
        if not isinstance(data, dict) or "repo" in data:
            return data
        if "repo_url" not in data:
            return data

        allowed_keys = {
            "repo_url",
            "branch_base",
            "task_title",
            "task_prompt",
            "task_external_id",
            "agent",
            "env_profile",
            "test_commands",
            "requires_database",
        }
        extras = {key: value for key, value in data.items() if key not in allowed_keys}
        profile_ref = (
            _LEGACY_DATABASE_PROFILE_REF
            if data.get("requires_database") is True
            else data.get("env_profile")
        )
        coerced: dict[str, object] = {
            **extras,
            "repo": {
                "url": data.get("repo_url"),
                "base_branch": data.get("branch_base", _LEGACY_FLAT_REPO_BASE_BRANCH_DEFAULT),
            },
            "task": {
                "title": data.get("task_title"),
                "prompt": data.get("task_prompt"),
                "agent": data.get("agent", AgentRuntime.codex),
                "external_id": data.get("task_external_id"),
                "kind": "feature_branch_pr",
            },
            "workspace": {"profile_ref": profile_ref or "auto", "profile": None},
            "validation": {"commands": data.get("test_commands", []), "requested_tier": 1},
            "resources": {},
        }
        return coerced

    @model_validator(mode="after")
    def _normalize_companions(self) -> WorkspaceCreateRequest:
        names: set[str] = set()
        companion_host_ports: dict[int, str] = {}
        normalized: list[WorkspaceCompanionRequest] = []
        for companion in self.companions:
            if companion.name in names:
                raise ValueError(f"duplicate companion name {companion.name!r}")
            names.add(companion.name)
            for _, host_port in companion.ports:
                previous_companion = companion_host_ports.get(host_port)
                if previous_companion is not None:
                    raise ValueError(
                        f"duplicate companion host port {host_port} requested by "
                        f"{previous_companion!r} and {companion.name!r}"
                    )
                companion_host_ports[host_port] = companion.name
            if companion.base_branch is None:
                companion = companion.model_copy(update={"base_branch": self.repo.base_branch})
            normalized.append(companion)
        self.companions = normalized
        return self

    @property
    def repo_url(self) -> str:
        return self.repo.url

    @property
    def branch_base(self) -> str:
        return self.repo.base_branch

    @property
    def task_title(self) -> str:
        return self.task.title

    @property
    def task_prompt(self) -> str:
        return self.task.prompt

    @property
    def task_external_id(self) -> str | None:
        return self.task.external_id

    @property
    def task_tag(self) -> str | None:
        return self.task.task_tag

    @property
    def agent(self) -> AgentRuntime:
        return self.task.agent

    @property
    def env_profile(self) -> str | None:
        return self.workspace.profile_ref

    @property
    def test_commands(self) -> list[str]:
        return self.validation.commands

    @property
    def requires_database(self) -> bool:
        return self.workspace.profile_ref == _LEGACY_DATABASE_PROFILE_REF


class PullRequestMonitorAdoptionRequest(BaseModel):
    """Input for adopting an already-open GitHub PR into AWF monitoring."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    repo_slug: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None

    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    model: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None
    effort: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None
    profile_ref: Annotated[str | None, Field(default="auto", max_length=128)] = "auto"
    profile: WorkspaceProfile | None = None
    owned_paths: list[OwnedPath] = Field(default_factory=list, max_length=128)
    auto_merge: bool = True
    initial_review_grace_period_seconds: float | None = Field(
        default=None,
        ge=0,
        le=86400,
    )
    task_title: Annotated[str | None, Field(default=None, min_length=1, max_length=512)] = None
    task_prompt: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=16384),
    ] = None
    task_tag: Annotated[str | None, Field(default=None, max_length=64)] = None
    reason: Annotated[str | None, Field(default=None, max_length=512)] = None

    @field_validator("task_tag")
    @classmethod
    def _validate_task_tag(cls, value: str | None) -> str | None:
        """Normalize + validate the optional Jira issue key; ``None`` when absent."""
        return validate_task_tag(value)


class PullRequestMonitorAdoptionResponse(BaseModel):
    """Response for the supported existing-PR adoption flow."""

    workspace_id: str
    status: WorkspaceStatus | str
    version: int
    task_id: str | None = None
    attempt_id: str | None = None
    candidate_id: str | None = None

    repo_slug: str
    repo_url: str
    pr_number: int
    pr_url: str
    head_ref: str
    base_ref: str
    head_sha: str | None = None
    base_sha: str | None = None
    auto_merge: bool
    monitor_policy: dict[str, Any] = Field(default_factory=dict)
    attached_existing: bool
    validation_provenance: ValidationFreshnessSummaryResponse = Field(
        default_factory=lambda: ValidationFreshnessSummaryResponse()
    )
    status_url: str
    events_url: str
    logs_url: str


class QueueDecisionSummaryResponse(BaseModel):
    id: str
    decision: str
    reason_code: str
    class_priority: int
    computed_priority: int
    age_boost: int
    retry_bonus: int
    resource_summary: dict[str, Any]
    overlap_risk_summary: dict[str, Any]
    score_summary: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime


class ResourceReservationSummaryResponse(BaseModel):
    id: str
    node_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int | None
    dind_slots: int
    phase: str
    reserved_at: datetime
    released_at: datetime | None


PolicyFindingSeverity = Literal["warning", "blocking"]
PolicyFindingStatus = Literal["active", "resolved"]
PolicyFindingReasonCode = Literal[
    "OUT_OF_SCOPE_CHANGE",
    "SUPPLY_CHAIN_UNPINNED_DEPENDENCY_INSTALL",
    "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION",
    "SUPPLY_CHAIN_UNEXPECTED_REGISTRY_HOST",
    "SUPPLY_CHAIN_LOCKFILE_OUTSIDE_OWNED_PATHS",
]


class PolicyFindingResponse(BaseModel):
    """Public projection of a structured workspace policy finding."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    candidate_id: str | None
    attempt_id: str | None
    task_id: str | None
    reason_code: PolicyFindingReasonCode
    severity: PolicyFindingSeverity
    subject_path: str | None
    explanation: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: PolicyFindingStatus
    detected_at: datetime
    resolved_at: datetime | None


class WorkspaceLifecycleStageResponse(BaseModel):
    stage: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    status: WorkspaceLifecycleStageStatus


class WorkspaceLlmUsageSummaryResponse(BaseModel):
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    cost_estimate: float | None = None
    currency: str | None = None
    status: LlmUsageStatus = "unavailable"
    source: str = "none"
    reason: str | None = "usage_not_reported"


class WorkspacePricingMetadataResponse(BaseModel):
    provider: str
    model: str
    currency: str
    unit: str
    price_per_unit: float | None = None
    timestamp: datetime
    version: int | None = None
    is_current: bool


class WorkspaceRecoveryCurrentOperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    payload: dict[str, Any] | None = None


class WorkspaceRecoverySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_state: str | None = None
    to_state: str | None = None
    reason_code: str | None = None
    action: str | None = None
    recovery_mode: str | None = None
    started_at: datetime
    current_operation: WorkspaceRecoveryCurrentOperationResponse | None = None
    summary: str
    payload: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


class ValidationRunSummaryResponse(BaseModel):
    validation_run_id: str
    attempt_id: str | None = None
    tier: ValidationTier
    command_set_hash: str
    base_commit: str | None = None
    base_sha: str | None = None
    workspace_head_sha: str | None = None
    target_branch: str | None = None
    target_head_sha: str | None = None
    current_target_head_sha: str | None = None
    profile_name: str | None = None
    profile_version: int | None = None
    profile_source: str | None = None
    resolved_profile_digest: str | None = None
    environment_identity_digest: str | None = None
    environment_identity_inputs: dict[str, Any] = Field(default_factory=dict)
    identity_source: ValidationIdentitySource = "legacy_fallback"
    status: ValidationProvenanceStatus
    reason_code: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    log_stream_refs: dict[str, Any] = Field(default_factory=dict)
    fresh_for_target: bool | None = None
    freshness_status: ValidationFreshnessStatus = "unknown"
    freshness_reason_code: str | None = None
    retry_count: int = 0
    coverage_percent: float | None = None
    coverage_minimum_percent: float | None = None
    coverage_status: str | None = None
    coverage_reason_code: str | None = None
    coverage_gaps: list[dict[str, Any]] = Field(default_factory=list)
    failing_test_node_ids: list[str] = Field(default_factory=list)
    failing_test_evidence: list[str] = Field(default_factory=list)


class ValidationFreshnessSummaryResponse(BaseModel):
    required_tier: ValidationTier | None = None
    latest_satisfied_tier: ValidationTier | None = None
    freshness_status: ValidationFreshnessStatus = "unknown"
    reason_code: str | None = None
    current_target_head_sha: str | None = None
    latest_validation: ValidationRunSummaryResponse | None = None


class WorkspaceRuntimeHealthResponse(BaseModel):
    status: Literal["ok", "stranded", "unavailable"]
    reason_code: str
    decision: Literal[
        "none",
        "fail_workspace",
        "remonitor_workspace",
        "defer_retry_policy",
        "preserve_runtime",
    ]
    message: str
    services: list[dict[str, str]] = Field(default_factory=list)


class WorkspaceSecretLeaseResponse(BaseModel):
    lease_id: str
    secret_name: str
    kind: str
    target: str
    status: str
    provider: str | None = None
    ref_digest: str | None = None
    issued_at: datetime
    mounted_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class WorkspaceSecretLeaseListResponse(BaseModel):
    items: list[WorkspaceSecretLeaseResponse] = Field(default_factory=list)


class WorkspaceAppEndpointHealthResponse(BaseModel):
    path: str
    method: Literal["GET", "HEAD"]
    expected_status: int
    internal_url: str


class WorkspaceAppEndpointResponse(BaseModel):
    name: str
    service: str
    scheme: Literal["http", "https"]
    port: int
    path: str
    internal_url: str
    visibility: Literal["agent", "validation", "console"]
    health: WorkspaceAppEndpointHealthResponse | None = None


class WorkspaceFailureConformanceResponse(BaseModel):
    summary: str | None = None
    gaps: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    report_reason_code: str | None = None
    iterations_used: int | None = None
    max_iterations: int | None = None
    plan_path: str | None = None
    report_path: str | None = None


class WorkspaceFailureSalvageResponse(BaseModel):
    hint: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    remote_push_branch: str | None = None


class WorkspaceFailurePlanningScopeResponse(BaseModel):
    scope_phase: str | None = None
    required_paths: list[str] = Field(default_factory=list)
    offending_paths: list[str] = Field(default_factory=list)
    offending_commands: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
    recovery_strategy: str | None = None
    salvage_policy: str | None = None
    plan_artifact: str | None = None
    fallback_model: dict[str, Any] | None = None


class FallbackTargetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent: str
    provider: str | None = None
    model: str


class ProviderRecoveryStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: Literal["retry", "fallback", "terminal"] | None = None
    reason_code: str | None = None
    source_provider: str | None = None
    source_model: str | None = None
    retry_attempt_number: int | None = None
    fallback_attempt_number: int | None = None
    cooldown_until: datetime | None = None
    next_eligible_at: datetime | None = None
    fallback_target: FallbackTargetResponse | None = None
    source_workspace_id: str | None = None
    source_attempt_id: str | None = None
    recommended_action: str | None = None
    terminal: bool | None = None


class WorkspaceFailureDetailsResponse(BaseModel):
    reason_code: str | None = None
    message: str | None = None
    conformance: WorkspaceFailureConformanceResponse | None = None
    salvage: WorkspaceFailureSalvageResponse | None = None
    planning_scope: WorkspaceFailurePlanningScopeResponse | None = None
    provider: str | None = None
    model: str | None = None
    retryable: bool | None = None
    recommended_action: str | None = None
    recovery_strategy: str | None = None
    salvage_policy: str | None = None
    fallback_model: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None
    failure_type: str | None = None
    retry_after_seconds: int | None = None
    cooldown_seconds: int | None = None
    failure_fingerprint: str | None = None
    fallback_allowed: bool | None = None
    provider_recovery_state: ProviderRecoveryStateResponse | None = None


class CoordinationOverlapResponse(BaseModel):
    workspace_id: str
    existing_path: str
    requested_path: str
    match_reason_code: str | None = None
    explanation: str | None = None


class WorkspaceCoordinationWarningResponse(BaseModel):
    warning_code: str
    message: str
    severity: WorkspaceOverlapGraphSeverity = "advisory"
    blocks_launch: bool = False
    workspace_ids: list[str] = Field(default_factory=list)
    overlaps: list[CoordinationOverlapResponse] = Field(default_factory=list)
    stale_policy_context: dict[str, str] = Field(default_factory=dict)
    overlap_count: int = 0
    overlaps_truncated: bool = False


class ProviderReadinessCredentialSourceResponse(BaseModel):
    type: str | None = None
    signal: str | None = None
    credential_scope: str | None = None
    isolation: str | None = None


class ProviderReadinessPreflightResponse(BaseModel):
    provider: str
    agent: str
    model: str | None = None
    model_source: str | None = None
    readiness_status: str
    auth_status: str
    auth_source: str
    credential_scope: str | None = None
    isolation: str | None = None
    probe_status: str
    reason_code: str
    message: str
    override_required: bool
    override_requested: bool = False
    override_used: bool
    override_reason: str | None = None
    blocks_launch: bool
    checked_at: datetime
    credential_sources: list[ProviderReadinessCredentialSourceResponse] = Field(
        default_factory=list
    )
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    probe_detail: str | None = None
    source_workspace_id: str | None = None


class WorkspaceBlockViolationResponse(BaseModel):
    """A single protected-file violation recorded when a workspace blocked.

    Mirrors ``quality_gate_violation_details`` so operators can see which paths
    triggered the pause and decide which ``guide --grant``/directive to issue.
    """

    model_config = ConfigDict(from_attributes=True)

    path: str | None = None
    protected_pattern: str | None = None
    section: str | None = None
    line: int | None = None
    reason: str | None = None


class WorkspaceBlockStateResponse(BaseModel):
    """Operator-facing block details surfaced while a workspace is ``blocked``.

    Projects the persisted ``block_*`` columns so ``GET /v1/workspaces/{id}``
    exposes the violating paths and block age, not just ``status=blocked``.
    """

    model_config = ConfigDict(from_attributes=True)

    block_type: str | None = None
    block_reason_code: str | None = None
    block_resume_phase: str | None = None
    block_epoch: int = 0
    blocked_at: datetime | None = None
    violations: list[WorkspaceBlockViolationResponse] = Field(default_factory=list)


class WorkspaceResponse(BaseModel):
    """Representation of a workspace in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: WorkspaceStatus
    version: int

    subphase: str | None = None
    last_activity_at: datetime | None = None
    last_log_at: datetime | None = None
    is_stale_running: bool = False

    repo_url: str
    branch_base: str
    branch_name: str | None
    base_commit: str | None

    task_title: str
    task_prompt: str
    task_external_id: str | None
    task_tag: str | None = None
    task_class: TaskClass | None
    owned_paths: list[str]
    task_policy: dict[str, Any] = Field(default_factory=dict)
    auto_merge: bool
    initial_review_grace_period_seconds: float | None

    agent: AgentRuntime
    agent_model: str | None = None
    agent_effort: str | None = None
    agent_model_source: AgentIdentitySource = "unavailable"
    agent_effort_source: AgentIdentitySource = "unavailable"
    env_profile: str | None
    profile_ref: str | None
    requested_profile: dict[str, Any] | None
    resolved_profile: dict[str, Any] | None
    network_posture: NetworkPosture | None = None

    test_commands: list[str]
    requires_database: bool

    node_id: str | None
    compose_project_name: str | None
    compose_file_path: str | None

    pr_url: str | None
    pr_number: int | None = None
    failure_reason: str | None
    failure_message: str | None
    failure_details: WorkspaceFailureDetailsResponse | None = None

    latest_queue_decision: QueueDecisionSummaryResponse | None = None
    active_resource_reservation: ResourceReservationSummaryResponse | None = None
    coordination_warnings: list[WorkspaceCoordinationWarningResponse] = Field(default_factory=list)
    policy_findings: list[PolicyFindingResponse] = Field(
        default_factory=list,
        validation_alias="active_policy_findings",
    )
    lifecycle: list[WorkspaceLifecycleStageResponse] = Field(default_factory=list)
    llm_usage: WorkspaceLlmUsageSummaryResponse = Field(
        default_factory=lambda: WorkspaceLlmUsageSummaryResponse()
    )
    pricing: WorkspacePricingMetadataResponse | None = None
    recovery: WorkspaceRecoverySummaryResponse | None = None
    validation_provenance: ValidationFreshnessSummaryResponse = Field(
        default_factory=lambda: ValidationFreshnessSummaryResponse()
    )
    runtime_health: WorkspaceRuntimeHealthResponse | None = None
    block_state: WorkspaceBlockStateResponse | None = None
    secret_leases: list[WorkspaceSecretLeaseResponse] = Field(default_factory=list)
    app_endpoints: list[WorkspaceAppEndpointResponse] = Field(default_factory=list)
    provider_recovery_state: ProviderRecoveryStateResponse | None = None
    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None
    egress_audit: EgressAuditRecordResponse | None = None

    created_at: datetime
    updated_at: datetime


class EgressAuditRecordResponse(BaseModel):
    """Immutable evidence of a workspace egress policy enforcement decision."""

    id: str
    workspace_id: str
    attempt_id: str | None = None
    policy_posture: str
    decision: str
    destination_category: str
    reason_code: str
    details: dict[str, Any] = Field(default_factory=dict)
    enforced_at: datetime
    created_at: datetime


class OwnedPathOverlapResponse(BaseModel):
    workspace_id: str
    existing_path: str
    requested_path: str


class WorkspaceLockOverlapRiskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overlapping_workspace_id: str
    overlapping_owned_path: str
    owned_path: str


class WorkspaceWarningResponse(BaseModel):
    warning_code: str
    message: str
    workspace_ids: list[str] = Field(default_factory=list)
    overlaps: list[OwnedPathOverlapResponse] = Field(default_factory=list)


class WorkspaceAcceptedResponse(BaseModel):
    """202 Accepted payload for workspace creation.

    Returned when provisioning hasn't completed within ``wait_timeout_seconds``.
    Clients poll ``status_url`` or subscribe to events.
    """

    workspace_id: str
    status: WorkspaceStatus
    version: int
    status_url: str
    events_url: str
    accepted_at: datetime
    warnings: list[WorkspaceWarningResponse] = Field(default_factory=list)
    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None


class WorkspaceRetryResponse(BaseModel):
    """202 Accepted payload for retrying a terminal workspace."""

    source_workspace_id: str
    new_workspace_id: str
    operation_id: str
    status: WorkspaceStatus
    attempt_number: int
    status_url: str
    events_url: str
    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None


class WorkspaceEventResponse(BaseModel):
    """Representation of an immutable workspace event."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    event_type: str
    old_state: str | None
    new_state: str | None
    reason_code: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime


class WorkspaceEventListResponse(BaseModel):
    """List envelope reserved for cursor pagination in a later slice."""

    items: list[WorkspaceEventResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class TaskResponse(BaseModel):
    """Workspace-backed task row for operator consoles."""

    task_id: str
    attempt_id: str | None = None
    attempt_number: int | None = None
    parent_attempt_id: str | None = None
    redispatch_from_attempt_id: str | None = None
    superseded_by_attempt_id: str | None = None
    is_canonical_for_merge: bool | None = None
    canonical_attempt_id: str | None = None
    candidate_id: str | None = None
    candidate_status: MergeCandidateStatus | None = None
    readiness: MergeCandidateReadinessResponse | None = None
    workspace_id: str
    title: str
    repo_url: str
    base_branch: str
    task_class: TaskClass | None
    owned_paths: list[str]
    agent: AgentRuntime
    agent_model: str | None = None
    agent_effort: str | None = None
    agent_model_source: AgentIdentitySource = "unavailable"
    agent_effort_source: AgentIdentitySource = "unavailable"
    llm_usage: WorkspaceLlmUsageSummaryResponse = Field(
        default_factory=lambda: WorkspaceLlmUsageSummaryResponse()
    )
    pricing: WorkspacePricingMetadataResponse | None = None
    status: WorkspaceStatus
    pr_url: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class TaskAttemptResponse(BaseModel):
    attempt_id: str
    task_id: str
    workspace_id: str
    attempt_number: int
    parent_attempt_id: str | None
    redispatch_from_attempt_id: str | None
    superseded_by_attempt_id: str | None
    is_canonical_for_merge: bool
    candidate_id: str | None = None
    candidate_status: MergeCandidateStatus | None = None
    readiness: MergeCandidateReadinessResponse | None = None
    agent: AgentRuntime
    status: WorkspaceStatus
    pr_url: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class TaskAttemptListResponse(BaseModel):
    task_id: str
    task_ref: str
    items: list[TaskAttemptResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceOverviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    task_id: str
    title: str
    task_prompt: str
    repo_url: str
    base_branch: str
    branch_name: str | None
    task_class: TaskClass | None
    owned_paths: list[str]
    agent: AgentRuntime
    agent_model: str | None = None
    agent_effort: str | None = None
    agent_model_source: AgentIdentitySource = "unavailable"
    agent_effort_source: AgentIdentitySource = "unavailable"
    network_posture: NetworkPosture | None = None
    lifecycle: list[WorkspaceLifecycleStageResponse] = Field(default_factory=list)
    llm_usage: WorkspaceLlmUsageSummaryResponse = Field(
        default_factory=lambda: WorkspaceLlmUsageSummaryResponse()
    )
    pricing: WorkspacePricingMetadataResponse | None = None
    recovery: WorkspaceRecoverySummaryResponse | None = None
    coordination_warnings: list[WorkspaceCoordinationWarningResponse] = Field(default_factory=list)
    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None
    status: WorkspaceStatus

    subphase: str | None = None
    last_activity_at: datetime | None = None
    last_log_at: datetime | None = None
    is_stale_running: bool = False
    current_phase: str
    active_operation: str | None
    last_event: WorkspaceEventResponse | None
    pr_url: str | None
    pr_number: int | None = None
    failure_reason: str | None
    failure_message: str | None
    created_at: datetime
    updated_at: datetime


class WorkspaceOverviewListResponse(BaseModel):
    items: list[WorkspaceOverviewResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


StaleReasonStatus = Literal["active", "resolved"]
StaleReasonCode = Literal[
    "STALE_TARGET_ADVANCED",
    "STALE_OVERLAP",
    "STALE_DEPENDENCY",
    "STALE_BUILD_CONFIG",
    "STALE_SCHEMA",
    "ADVISORY_PLAN_ARTIFACT_OVERLAP",
]
StaleReasonTrigger = Literal[
    "target_advanced",
    "path_overlap",
    "schema_changed",
    "dependency_changed",
    "build_config_changed",
    "plan_artifact_overlap",
]
StaleReasonSeverity = Literal["blocking", "advisory"]


class StaleReasonResponse(BaseModel):
    """Public projection of one ``stale_reasons`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    candidate_id: str | None
    attempt_id: str | None
    task_id: str | None
    trigger_type: StaleReasonTrigger
    trigger_ref: str | None
    reason_code: StaleReasonCode
    explanation: str
    status: StaleReasonStatus
    severity: StaleReasonSeverity
    blocks_merge: bool
    detected_at: datetime
    resolved_at: datetime | None


class StaleReasonListResponse(BaseModel):
    items: list[StaleReasonResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class MergeQueueBlockerResponse(BaseModel):
    candidate_id: str
    workspace_id: str
    attempt_id: str
    task_id: str
    title: str
    pr_url: str
    pr_number: int | None
    status: WorkspaceStatus
    blocker_state: MergeQueueBlockerState
    reason_code: str


class MergeQueueItemResponse(BaseModel):
    candidate_id: str | None = None
    candidate_status: MergeCandidateStatus | None = None
    close_reason: str | None = None
    attempt_id: str | None = None
    task_id: str
    workspace_id: str
    title: str
    repo_url: str
    base_branch: str
    branch_name: str | None
    pr_url: str
    status: WorkspaceStatus
    auto_merge: bool
    task_class: TaskClass | None
    owned_paths: list[str]
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None
    last_event: WorkspaceEventResponse | None
    merge_blocker_reason: MergeBlockerReason
    required_next_action: str | None = None
    required_validation_tier: ValidationTier | None = None
    latest_satisfied_validation_tier: ValidationTier | None = None
    validation_freshness_status: ValidationFreshnessStatus = "unknown"
    validation_reason_code: str | None = None
    readiness: MergeCandidateReadinessResponse | None = None
    canonical: bool
    queue_blockers: list[MergeQueueBlockerResponse] = Field(default_factory=list)
    latest_validation: ValidationRunSummaryResponse | None = None
    stale_reasons: list[StaleReasonResponse] = Field(default_factory=list)
    policy_findings: list[PolicyFindingResponse] = Field(default_factory=list)
    provider_recovery_state: ProviderRecoveryStateResponse | None = None


class MergeQueueListResponse(BaseModel):
    items: list[MergeQueueItemResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceLockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    title: str
    agent: AgentRuntime
    status: WorkspaceStatus
    repo_url: str
    branch_base: str
    task_class: TaskClass | None
    owned_paths: list[str]
    overlap_risks: list[WorkspaceLockOverlapRiskResponse] = Field(default_factory=list)
    pr_url: str | None
    created_at: datetime
    updated_at: datetime


class WorkspaceLockListResponse(BaseModel):
    items: list[WorkspaceLockResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceOverlapGraphNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    title: str
    status: WorkspaceStatus
    queue_state: WorkspaceOverlapGraphQueueState
    repo_url: str
    branch_base: str
    task_class: TaskClass | None
    owned_paths: list[str]
    created_at: datetime
    updated_at: datetime


class WorkspaceOverlapPathMatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    left_workspace_id: str
    left_owned_path: str
    right_workspace_id: str
    right_owned_path: str
    match_reason_code: WorkspaceOverlapPathMatchReasonCode
    explanation: str


class WorkspaceOverlapGraphEdgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    left_workspace_id: str
    right_workspace_id: str
    repo_url: str
    branch_base: str
    reason_code: WorkspaceOverlapGraphReasonCode
    severity: WorkspaceOverlapGraphSeverity
    blocks_launch: bool
    affected_workspace_ids: list[str]
    path_match_count: int
    path_matches_truncated: bool
    path_matches: list[WorkspaceOverlapPathMatchResponse]


class WorkspaceOverlapGraphSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_count: int
    queued_count: int
    running_count: int
    edge_count: int
    affected_workspace_count: int
    has_more: bool


class WorkspaceOverlapGraphResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    nodes: list[WorkspaceOverlapGraphNodeResponse]
    edges: list[WorkspaceOverlapGraphEdgeResponse]
    summary: WorkspaceOverlapGraphSummaryResponse


class RuntimeServiceResponse(BaseModel):
    name: str
    container_id: str | None = None
    image: str | None = None
    state: str
    status: str | None = None
    health: str | None = None
    ports: list[str] = Field(default_factory=list)
    started_at: str | None = None


class WorkspaceRuntimeResponse(BaseModel):
    workspace_id: str
    compose_project_name: str | None
    stack_state: str
    services: list[RuntimeServiceResponse] = Field(default_factory=list)
    logs_available: bool
    control_available: bool
    reason: str | None = None
    runtime_health: WorkspaceRuntimeHealthResponse | None = None
    app_endpoints: list[WorkspaceAppEndpointResponse] = Field(default_factory=list)


class WorkspaceLogStreamResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    source: str
    name: str
    kind: str
    path: str
    byte_count: int
    line_count: int
    opened_at: datetime
    closed_at: datetime | None


class WorkspaceLogListResponse(BaseModel):
    items: list[WorkspaceLogStreamResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceLogReadResponse(BaseModel):
    stream_id: str
    offset: int
    next_offset: int
    eof: bool
    data: str


class WorkspaceArtifactResponse(BaseModel):
    artifact_id: str
    workspace_id: str
    name: str
    relative_path: str
    path: str
    kind: str
    size_bytes: int
    modified_at: datetime


class WorkspaceArtifactListResponse(BaseModel):
    items: list[WorkspaceArtifactResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceArtifactReadResponse(BaseModel):
    """Bounded artifact bytes + metadata returned by ``awf_read_workspace_artifact``."""

    workspace_id: str
    relative_path: str
    name: str
    content_type: str
    size_bytes: int
    content: str
    """Base64-encoded artifact bytes (standard alphabet, no line breaks)."""


class ValidationProvenanceItemResponse(BaseModel):
    validation_run_id: str | None = None
    workspace_id: str
    attempt_id: str | None = None
    tier: ValidationTier | None = None
    command_set_hash: str | None = None
    phase: str
    command_index: int
    command: str | None
    stream_ids: dict[str, str | None]
    stdout_byte_count: int
    stdout_line_count: int
    stderr_byte_count: int
    stderr_line_count: int
    opened_at: datetime
    closed_at: datetime | None
    status: ValidationProvenanceStatus
    reason_code: str | None = None
    base_commit: str | None
    base_sha: str | None = None
    workspace_head_sha: str | None = None
    branch_name: str | None
    target_branch: str | None = None
    target_head_sha: str | None = None
    current_target_head_sha: str | None = None
    profile_name: str | None = None
    profile_version: int | None = None
    profile_source: str | None = None
    resolved_profile_digest: str | None = None
    environment_identity_digest: str | None = None
    environment_identity_inputs: dict[str, Any] = Field(default_factory=dict)
    identity_source: ValidationIdentitySource = "legacy_fallback"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_stream_refs: dict[str, Any] = Field(default_factory=dict)
    fresh_for_target: bool | None = None
    retry_count: int = 0
    coverage_percent: float | None = None
    coverage_minimum_percent: float | None = None
    coverage_status: str | None = None
    coverage_reason_code: str | None = None
    coverage_gaps: list[dict[str, Any]] = Field(default_factory=list)
    failing_test_node_ids: list[str] = Field(default_factory=list)
    failing_test_evidence: list[str] = Field(default_factory=list)


class ValidationProvenanceListResponse(BaseModel):
    items: list[ValidationProvenanceItemResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


_merge_log_stream_ref_value = merge_log_stream_ref_value
_log_stream_ids = log_stream_ids
