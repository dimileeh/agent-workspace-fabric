"""Resource capacity summary tests."""

from __future__ import annotations

import pytest

from awf.common.config import Settings
from awf.service.disk import DiskCheck
from awf.service.resource_capacity import (
    LocalCapacityLimits,
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
def test_detected_local_capacity_fills_unset_cpu_and_memory_limits() -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=None,
        local_capacity_memory_gb=None,
        local_capacity_dind_slots=None,
    )

    summary = resource_capacity_summary(
        settings=settings,
        reserved=ReservedResources(
            active_workspace_count=1,
            steady_cpu=3.0,
            steady_memory_gb=10.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=0,
        ),
        resource_defaults=WorkspaceResourceDefaults(
            steady_cpu=3.0,
            steady_memory_gb=10.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
        ),
        disk_check=None,
        detected_local_capacity=LocalCapacityLimits(cpu_cores=8.0, memory_gb=24.0),
    )

    assert summary.steady_cpu.limit == 8.0
    assert summary.steady_cpu.available == 5.0
    assert summary.peak_cpu.limit == 8.0
    assert summary.peak_cpu.available_after_next_default == 0.0
    assert summary.steady_memory_gb.limit == 24.0
    assert summary.steady_memory_gb.available == 14.0
    assert summary.peak_memory_gb.limit == 24.0
    assert summary.peak_memory_gb.available_after_next_default == 0.0
    assert "STEADY_CPU_CAPACITY_UNKNOWN" not in summary.pressure_reasons
    assert "PEAK_MEMORY_CAPACITY_UNKNOWN" not in summary.pressure_reasons


@pytest.mark.unit
def test_configured_local_capacity_overrides_detected_limits() -> None:
    settings = Settings(
        _env_file=None,
        local_capacity_cpu_cores=12.0,
        local_capacity_memory_gb=32.0,
        local_capacity_dind_slots=None,
    )

    summary = resource_capacity_summary(
        settings=settings,
        reserved=_reserved_resources(),
        resource_defaults=_resource_defaults(),
        disk_check=None,
        detected_local_capacity=LocalCapacityLimits(cpu_cores=8.0, memory_gb=24.0),
    )

    assert summary.peak_cpu.limit == 12.0
    assert summary.peak_memory_gb.limit == 32.0


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

    assert summary.disk_mb.limit == 16 * 1024
    assert summary.disk_mb.available == 2 * 1024
    assert summary.disk_mb.available_after_next_default is None
    assert summary.disk_mb.reason_code == "INSUFFICIENT_DISK"
    assert summary.pressure_reasons == ("INSUFFICIENT_DISK",)
