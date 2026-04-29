"""Pydantic schemas for the public API.

These schemas define the HTTP contract and are reused by the MCP server so
REST + MCP stay in lockstep. Keep them narrow: business objects live in
``awf.db.models``, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from awf.db.enums import AgentRuntime, OperationStatus, TaskClass, WorkspaceStatus
from awf.profiles.models import OutOfScopeChangePolicy, WorkspaceProfile

OwnedPath = Annotated[str, Field(min_length=1, max_length=512)]
MergeBlockerReason = Literal[
    "ready_to_merge_or_waiting_for_github",
    "manual_merge_required",
    "waiting_for_monitor",
    "waiting_for_older_candidate",
    "workspace_not_terminal",
    "completed",
    "failed_or_cancelled",
    "not_canonical",
    "policy_blocked",
    "stale",
]
MergeCandidateStatus = Literal["open", "merged", "closed"]
MergeQueueBlockerState = Literal["merge_eligible", "monitor_owned_recovery"]
ValidationTier = Literal[1, 2, 3]
ValidationProvenanceStatus = Literal["running", "succeeded", "failed", "unknown"]
ValidationFreshnessStatus = Literal["fresh", "stale", "unknown", "unavailable"]
ValidationIdentitySource = Literal["persisted", "legacy_fallback"]
AgentIdentitySource = Literal["task_policy", "default", "unavailable"]
WorkspaceLifecycleStageStatus = Literal[
    "pending",
    "active",
    "completed",
    "terminal_skipped",
]
LlmUsageStatus = Literal["available", "unavailable"]
WorkspaceOverlapGraphQueueState = Literal["queued", "running"]
WorkspaceOverlapGraphSeverity = Literal["advisory"]
WorkspaceOverlapGraphReasonCode = Literal["OWNED_PATH_OVERLAP_RISK"]
WorkspaceOverlapPathMatchReasonCode = Literal[
    "OWNED_PATH_EXACT_MATCH",
    "OWNED_PATH_ANCESTOR_MATCH",
    "OWNED_PATH_WILDCARD_MATCH",
]

_MAX_LOG_STREAM_REF_DEPTH = 64


class MergeCandidateReadinessResponse(BaseModel):
    ready: bool
    manual_merge_required: bool
    waiting_for_monitor: bool
    failed_or_cancelled: bool
    completed: bool
    not_canonical: bool
    stale: bool
    stale_reason: str | None = None


class WorkspaceCreateRequest(BaseModel):
    """Input for ``POST /v1/workspaces`` and ``awf_create_workspace`` (MCP).

    Fields are grouped logically; see docs/PLAN_MVP.md § API surface.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: Annotated[str, Field(min_length=1, max_length=512)]
    branch_base: Annotated[str, Field(default="development", min_length=1, max_length=256)]

    task_title: Annotated[str, Field(min_length=1, max_length=512)]
    task_prompt: Annotated[str, Field(min_length=1, max_length=16384)]
    task_external_id: Annotated[str | None, Field(default=None, max_length=128)]

    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    env_profile: Annotated[str | None, Field(default=None, max_length=128)]

    test_commands: list[str] = Field(default_factory=list)
    requires_database: bool = False


class WorkspaceV2Repo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: Annotated[str, Field(min_length=1, max_length=512)]
    base_branch: Annotated[str, Field(default="main", min_length=1, max_length=256)]


class WorkspaceV2Task(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Annotated[str, Field(min_length=1, max_length=512)]
    prompt: Annotated[str, Field(min_length=1, max_length=16384)]
    kind: Annotated[str, Field(default="feature_branch_pr", max_length=32)]
    agent: AgentRuntime = Field(default=AgentRuntime.codex)
    model: Annotated[str | None, Field(default=None, min_length=1, max_length=128)] = None
    external_id: Annotated[str | None, Field(default=None, max_length=128)]
    task_class: TaskClass | None = None
    priority: int = Field(default=0, ge=0, le=100)
    owned_paths: list[OwnedPath] = Field(default_factory=list, max_length=128)
    out_of_scope_changes: OutOfScopeChangePolicy | None = None
    auto_merge: bool = True
    initial_review_grace_period_seconds: float | None = Field(
        default=None,
        ge=0,
        le=86400,
    )


class WorkspaceV2Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_ref: Annotated[str | None, Field(default="auto", max_length=128)]
    profile: WorkspaceProfile | None = None


class WorkspaceV2Validation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    commands: list[str] = Field(default_factory=list)
    requested_tier: int = Field(default=1, ge=1, le=3)


class WorkspaceV2Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cpu: float | None = Field(default=None, gt=0)
    memory: Annotated[str | None, Field(default=None, max_length=32)]
    steady_state_cpu_cores: float | None = Field(default=None, gt=0)
    steady_state_memory_gb: float | None = Field(default=None, gt=0)
    peak_cpu_cores: float | None = Field(default=None, gt=0)
    peak_memory_gb: float | None = Field(default=None, gt=0)
    disk_mb: int | None = Field(default=None, gt=0)


class WorkspaceCreateV2Request(BaseModel):
    """Clean v2 workspace creation contract."""

    model_config = ConfigDict(extra="forbid")

    repo: WorkspaceV2Repo
    task: WorkspaceV2Task
    workspace: WorkspaceV2Workspace = Field(
        default_factory=lambda: WorkspaceV2Workspace(profile_ref="auto", profile=None)
    )
    validation: WorkspaceV2Validation = Field(default_factory=lambda: WorkspaceV2Validation())
    resources: WorkspaceV2Resources = Field(
        default_factory=lambda: WorkspaceV2Resources(cpu=None, memory=None)
    )


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
    decided_at: datetime


class ResourceReservationSummaryResponse(BaseModel):
    id: str
    node_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int | None
    phase: str
    reserved_at: datetime
    released_at: datetime | None


PolicyFindingSeverity = Literal["warning", "blocking"]
PolicyFindingStatus = Literal["active", "resolved"]
PolicyFindingReasonCode = Literal["OUT_OF_SCOPE_CHANGE"]


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
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_estimate: float | None = None
    currency: str | None = None
    status: LlmUsageStatus = "unavailable"
    source: str = "none"
    reason: str | None = "usage_not_reported"


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
    ]
    message: str
    services: list[dict[str, str]] = Field(default_factory=list)


class WorkspaceResponse(BaseModel):
    """Representation of a workspace in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    status: WorkspaceStatus
    version: int

    repo_url: str
    branch_base: str
    branch_name: str | None
    base_commit: str | None

    task_title: str
    task_prompt: str
    task_external_id: str | None
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

    test_commands: list[str]
    requires_database: bool

    node_id: str | None
    compose_project_name: str | None
    compose_file_path: str | None

    pr_url: str | None
    failure_reason: str | None
    failure_message: str | None

    latest_queue_decision: QueueDecisionSummaryResponse | None = None
    active_resource_reservation: ResourceReservationSummaryResponse | None = None
    policy_findings: list[PolicyFindingResponse] = Field(
        default_factory=list,
        validation_alias="active_policy_findings",
    )
    lifecycle: list[WorkspaceLifecycleStageResponse] = Field(default_factory=list)
    llm_usage: WorkspaceLlmUsageSummaryResponse = Field(
        default_factory=lambda: WorkspaceLlmUsageSummaryResponse()
    )
    recovery: WorkspaceRecoverySummaryResponse | None = None
    validation_provenance: ValidationFreshnessSummaryResponse = Field(
        default_factory=lambda: ValidationFreshnessSummaryResponse()
    )
    runtime_health: WorkspaceRuntimeHealthResponse | None = None

    created_at: datetime
    updated_at: datetime


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


class WorkspaceRetryResponse(BaseModel):
    """202 Accepted payload for retrying a terminal workspace."""

    source_workspace_id: str
    new_workspace_id: str
    operation_id: str
    status: WorkspaceStatus
    attempt_number: int
    status_url: str
    events_url: str


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
    lifecycle: list[WorkspaceLifecycleStageResponse] = Field(default_factory=list)
    llm_usage: WorkspaceLlmUsageSummaryResponse = Field(
        default_factory=lambda: WorkspaceLlmUsageSummaryResponse()
    )
    recovery: WorkspaceRecoverySummaryResponse | None = None
    status: WorkspaceStatus
    current_phase: str
    active_operation: str | None
    last_event: WorkspaceEventResponse | None
    pr_url: str | None
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


class ValidationProvenanceListResponse(BaseModel):
    items: list[ValidationProvenanceItemResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: str
    type: str
    status: str
    error_code: str | None
    error_message: str | None
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    idempotency_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    owner: str | None = None
    source: str | None = None
    action: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    source_head_sha: str | None = None
    source_base_sha: str | None = None
    reason: str | None = None
    reason_code: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    log_stream_refs: dict[str, Any] = Field(default_factory=dict)
    log_stream_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_audit_fields(self) -> OperationResponse:
        payload = self.payload if isinstance(self.payload, dict) else {}
        result = self.result if isinstance(self.result, dict) else {}
        self.owner = self.owner or _first_str(payload, result, key="owner")
        self.source = self.source or _first_str(payload, result, key="source")
        self.action = self.action or _first_str(payload, result, key="action")
        self.pr_number = self.pr_number or _first_int(payload, result, key="pr_number")
        self.pr_url = self.pr_url or _first_str(payload, result, key="pr_url")
        self.source_head_sha = self.source_head_sha or _first_str(
            payload, result, key="source_head_sha"
        )
        self.source_base_sha = self.source_base_sha or _first_str(
            payload, result, key="source_base_sha"
        )
        self.reason = self.reason or _first_str(payload, result, key="reason")
        self.reason_code = self.reason_code or _first_str(payload, result, key="reason_code")
        self.failure_code = (
            self.failure_code
            or _first_str(result, payload, key="failure_code")
            or _first_str(result, payload, key="error_code")
            or self.error_code
        )
        self.failure_message = (
            self.failure_message
            or _first_str(result, payload, key="failure_message")
            or _first_str(result, payload, key="error_message")
            or self.error_message
        )
        refs = _merge_log_stream_refs(
            self.log_stream_refs,
            _operation_log_stream_refs(payload, result),
        )
        self.log_stream_refs = refs
        self.log_stream_ids = _log_stream_ids(self.log_stream_refs)
        return self


def _first_str(*sources: dict[str, Any], key: str) -> str | None:
    for source in sources:
        value = source.get(key)
        if isinstance(value, str):
            return value
    return None


def _first_int(*sources: dict[str, Any], key: str) -> int | None:
    for source in sources:
        value = source.get(key)
        if isinstance(value, int):
            return value
    return None


def _operation_log_stream_refs(
    *sources: dict[str, Any],
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for source in sources:
        value = source.get("log_stream_refs")
        if isinstance(value, dict):
            refs = _merge_log_stream_refs(refs, value)
    return refs


def _merge_log_stream_refs(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    refs = dict(existing)
    for key, value in incoming.items():
        if key in refs:
            refs[key] = _merge_log_stream_ref_value(refs[key], value)
        else:
            refs[key] = value
    return refs


def _merge_log_stream_ref_value(existing: Any, incoming: Any) -> Any:
    if existing == incoming:
        return existing
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return _merge_log_stream_refs(existing, incoming)

    values = list(existing) if isinstance(existing, list) else [existing]
    incoming_values = incoming if isinstance(incoming, list) else [incoming]
    for value in incoming_values:
        if value not in values:
            values.append(value)
    return values


def _log_stream_ids(value: Any) -> list[str]:
    ids: set[str] = set()

    def collect(item: Any, depth: int = 0) -> None:
        if depth > _MAX_LOG_STREAM_REF_DEPTH:
            return
        if isinstance(item, str):
            ids.add(item)
            return
        if isinstance(item, dict):
            for child in item.values():
                collect(child, depth + 1)
            return
        if isinstance(item, list | tuple):
            for child in item:
                collect(child, depth + 1)

    collect(value)
    return sorted(ids)


class OperationListResponse(BaseModel):
    items: list[OperationResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str | None, Field(default=None, max_length=1024)]
    stop_stack: bool = True


class WorkspaceOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str | None, Field(default=None, max_length=1024)]
    requested_tier: Annotated[int | None, Field(default=None, ge=1, le=3)]


class WorkspaceControlResponse(BaseModel):
    workspace_id: str
    operation_id: str
    operation_status: OperationStatus
    status: WorkspaceStatus
    message: str


class ErrorResponse(BaseModel):
    """Uniform error envelope.

    See docs/PLAN_MVP.md § Error code taxonomy for the canonical error codes.
    """

    error_code: str
    message: str
    detail: dict[str, Any] | None = None
