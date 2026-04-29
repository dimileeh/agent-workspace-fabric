"""Resource capacity summary tests."""

from __future__ import annotations

import pytest

from awf.common.config import Settings
from awf.service.disk import DiskCheck
from awf.service.resource_capacity import (
    ReservedResources,
    WorkspaceResourceDefaults,
    resource_capacity_summary,
)

_MIB = 1024 * 1024


def _resource_defaults() -> WorkspaceResourceDefaults:
    return WorkspaceResourceDefaults(
        steady_cpu=0.0,
        steady_memory_gb=0.0,
        peak_cpu=0.0,
        peak_memory_gb=0.0,
    )


def _reserved_resources(*, disk_mb: int = 0) -> ReservedResources:
    return ReservedResources(
        active_workspace_count=0,
        steady_cpu=0.0,
        steady_memory_gb=0.0,
        peak_cpu=0.0,
        peak_memory_gb=0.0,
        disk_mb=disk_mb,
        dind_slots=0,
    )


@pytest.mark.unit
def test_disk_capacity_reports_admission_failure_separately_from_reservations() -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=8.0,
        local_capacity_memory_gb=16.0,
        local_capacity_dind_slots=1,
    )
    disk_check = DiskCheck(
        path="/tmp/awf-work",
        checked_path="/tmp",
        total_bytes=16 * 1024 * _MIB,
        used_bytes=10 * 1024 * _MIB,
        free_bytes=6 * 1024 * _MIB,
        percent_free=37.5,
        threshold_bytes=8 * 1024 * _MIB,
        ok=False,
        status="fail",
        reason="INSUFFICIENT_DISK",
        detail="Free disk is below the configured admission threshold.",
    )

    summary = resource_capacity_summary(
        settings=settings,
        reserved=_reserved_resources(disk_mb=4 * 1024),
        resource_defaults=_resource_defaults(),
        disk_check=disk_check,
    )

    assert summary.disk_mb.available == 2 * 1024
    assert summary.disk_mb.reason_code == "INSUFFICIENT_DISK"
    assert summary.pressure_reasons == ("INSUFFICIENT_DISK",)
