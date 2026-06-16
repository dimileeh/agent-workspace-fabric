"""Shared dataclasses for AWF service metrics summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from awf.service.disk import DiskCheck
from awf.service.orphan_resources import OrphanResourceSummary
from awf.service.resource_capacity import (
    ReservedResources,
    ResourceCapacitySummary,
    WorkspaceResourceDefaults,
)
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary


@dataclass(frozen=True)
class WorkspaceReliabilitySummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int
    status_counts: dict[str, int]
    failure_reason_counts: dict[str, int]
    active_count: int
    destroying_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    destroyed_count: int
    cleanup_failure_count: int
    stuck_count: int
    actionable_reason_count: int
    unactionable_reason_count: int


@dataclass(frozen=True)
class FailureAction:
    retryable: bool
    recommended_action: str


@dataclass(frozen=True)
class FailureReasonGroup:
    failure_reason: str
    count: int
    retryable: bool
    recommended_action: str


@dataclass(frozen=True)
class FailedWorkspaceExample:
    workspace_id: str
    title: str
    repo_url: str
    branch_base: str
    agent: str
    status: str
    failure_reason: str
    failure_message: str | None
    pr_url: str | None
    created_at: datetime
    updated_at: datetime
    reason_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class RootCauseCluster:
    agent: str
    agent_model: str | None
    failure_reason: str
    reason_code: str | None
    likely_cause: str
    actionable_next_action: str
    count: int
    sample_workspace_ids: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class FailureAnalysisSummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int
    total_failed_workspaces: int
    failure_groups: list[FailureReasonGroup]
    latest_examples: list[FailedWorkspaceExample]
    root_cause_clusters: list[RootCauseCluster] = field(default_factory=list)


@dataclass(frozen=True)
class SloMetricsSummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int

    creation_total: int
    creation_succeeded: int
    creation_failed: int
    creation_cancelled: int

    cleanup_total: int
    cleanup_succeeded: int
    cleanup_failure_count: int

    stuck_running_count: int
    stuck_with_reason_count: int

    recovery_total: int
    recovery_succeeded: int
    recovery_failed_count: int

    monitor_completed_total: int
    completed_after_monitor_count: int
    monitor_stuck_count: int

    actionable_failure_count: int
    unactionable_failure_count: int


@dataclass(frozen=True)
class WorkspaceSaturationCounts:
    by_status: dict[str, int]
    active_total: int
    requested: int
    provisioning: int
    ready: int
    running: int
    validating: int
    pushing: int
    monitoring_pr: int
    blocked: int
    destroying: int
    completed: int
    failed: int
    cancelled: int
    destroyed: int


@dataclass(frozen=True)
class WorkerConcurrencySettings:
    max_concurrent_provisions: int
    max_concurrent_executions: int


@dataclass(frozen=True)
class ConcurrencyLane:
    limit: int
    in_use: int
    queued: int
    available: int


@dataclass(frozen=True)
class ResourceConcurrency:
    provision: ConcurrencyLane
    execution: ConcurrencyLane


@dataclass(frozen=True)
class AdmissionSummary:
    ok: bool
    status: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class CapacityQueueSummary:
    queued_workspace_count: int
    oldest_workspace_id: str | None
    oldest_wait_seconds: int | None
    planned_resources: ReservedResources
    blocked_reason_counts: dict[str, int]


@dataclass(frozen=True)
class _AllocatedResourceAuxiliaryCounts:
    unreserved_workspace_count: int
    defaulted_dind_slots: int


@dataclass(frozen=True)
class _CapacityQueueWorkspace:
    id: str
    agent: str
    task_class: str | None
    task_policy: dict[str, Any]
    created_at: datetime
    resolved_profile: object


@dataclass(frozen=True)
class _CapacityQueueCandidate:
    workspace: _CapacityQueueWorkspace
    demand: ReservedResources


@dataclass
class _CapacityQueueAllocated:
    active_workspace_count: int
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int = 0
    dind_slots: int = 0

    @classmethod
    def from_reserved(cls, resources: ReservedResources) -> _CapacityQueueAllocated:
        return cls(
            active_workspace_count=resources.active_workspace_count,
            steady_cpu=resources.steady_cpu,
            steady_memory_gb=resources.steady_memory_gb,
            peak_cpu=resources.peak_cpu,
            peak_memory_gb=resources.peak_memory_gb,
            disk_mb=resources.disk_mb,
            dind_slots=resources.dind_slots,
        )

    def add(self, demand: ReservedResources) -> None:
        self.active_workspace_count += demand.active_workspace_count
        self.steady_cpu += demand.steady_cpu
        self.steady_memory_gb += demand.steady_memory_gb
        self.peak_cpu += demand.peak_cpu
        self.peak_memory_gb += demand.peak_memory_gb
        self.disk_mb += demand.disk_mb
        self.dind_slots += demand.dind_slots


@dataclass(frozen=True)
class ProviderCircuitBreakerSummary:
    provider: str
    model: str
    state: str
    failure_count: int
    cooldown_until: datetime | None
    last_reason_code: str | None
    last_workspace_id: str | None


@dataclass(frozen=True)
class ProviderRecoveryStateSummary:
    pending_retry: int
    pending_fallback: int
    in_cooldown: int
    terminal_no_loop: int
    terminal_exhausted: int
    circuit_breakers_open: int


@dataclass(frozen=True)
class LocalCapacitySourceSummary:
    """Runtime capacity origin metadata used by saturation summaries."""

    cpu_cores: float | None
    memory_gb: float | None
    source: str | None
    reason_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ResourceSaturationSummary:
    generated_at: datetime
    workspace_counts: WorkspaceSaturationCounts
    worker: WorkerConcurrencySettings
    local_capacity: LocalCapacitySourceSummary
    resource_defaults: WorkspaceResourceDefaults
    reserved_resources: ReservedResources
    capacity: ResourceCapacitySummary
    allocated_resources: ReservedResources
    allocated_capacity: ResourceCapacitySummary
    capacity_queue: CapacityQueueSummary
    concurrency: ResourceConcurrency
    disk: DiskCheck
    orphan_resources: OrphanResourceSummary
    runtime_health: WorkspaceRuntimeHealthSummary
    admission: AdmissionSummary
    provider_circuit_breakers: list[ProviderCircuitBreakerSummary] = field(default_factory=list)
    provider_recovery_state_summary: ProviderRecoveryStateSummary | None = None
