"""Control-worker configuration."""

from __future__ import annotations

from dataclasses import dataclass

from awf.service.config import (
    DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID as DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
)
from awf.service.gc import DEFAULT_MIN_AGE_HOURS


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: float = 1.0
    max_concurrent_provisions: int = 3
    max_concurrent_executions: int = 3
    monitor_claim_lease_seconds: float = 300.0
    execution_claim_lease_seconds: float = 300.0
    stale_active_execution_scan_interval_seconds: float = 300.0
    active_execution_preservation_grace_seconds: float = 900.0
    secret_lease_expiration_scan_interval_seconds: float = 60.0
    terminal_runtime_release_scan_interval_seconds: float = 300.0
    terminal_runtime_release_max_per_scan: int = 5
    orphan_reconcile_scan_interval_seconds: float = 3600.0
    orphan_reconcile_max_per_scan: int = 50
    orphan_reconcile_min_age_hours: float = DEFAULT_MIN_AGE_HOURS
    auto_cleanup_orphans: bool = False
    node_id: str | None = None
    local_capacity_cpu_cores: float | None = None
    local_capacity_memory_gb: float | None = None
    local_capacity_dind_slots: int | None = None
    workspace_steady_cpu: float = 3.0
    workspace_steady_memory_gb: float = 10.0
    workspace_peak_cpu: float = 6.0
    workspace_peak_memory_gb: float = 16.0


def effective_worker_config_node_id(config: WorkerConfig) -> str:
    return config.node_id or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
