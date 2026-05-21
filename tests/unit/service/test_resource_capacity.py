"""Resource capacity summary tests."""

from __future__ import annotations

import pytest

import awf.service.resource_capacity as resource_capacity
from awf.common.config import Settings
from awf.service.disk import DiskCheck
from awf.service.resource_capacity import (
    LOCAL_CAPACITY_CONSTRAINTS,
    LocalCapacityConstraint,
    LocalCapacityLimits,
    ReservedResources,
    WorkspaceResourceDefaults,
    default_dind_slots_from_profile,
    local_capacity_blocker,
    local_capacity_limit,
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
def test_default_dind_slots_from_profile_detects_dind_mode() -> None:
    assert default_dind_slots_from_profile({"docker": {"mode": "dind"}}) == 1
    assert default_dind_slots_from_profile({"docker": {"mode": "host"}}) == 0
    assert default_dind_slots_from_profile({"docker": {}}) == 0
    assert default_dind_slots_from_profile(None) == 0


@pytest.mark.unit
def test_local_capacity_constraints_share_reason_codes_and_limit_sources() -> None:
    assert [
        (
            constraint.dimension,
            constraint.reason_code,
            local_capacity_limit(
                constraint,
                cpu_limit=8.0,
                memory_limit=24.0,
                dind_slots=1,
            ),
        )
        for constraint in LOCAL_CAPACITY_CONSTRAINTS
    ] == [
        ("steady_cpu", "STEADY_CPU_CAPACITY_SATURATED", 8.0),
        ("peak_cpu", "PEAK_CPU_CAPACITY_SATURATED", 8.0),
        ("steady_memory_gb", "STEADY_MEMORY_CAPACITY_SATURATED", 24.0),
        ("peak_memory_gb", "PEAK_MEMORY_CAPACITY_SATURATED", 24.0),
        ("dind_slots", "DIND_CAPACITY_SATURATED", 1),
    ]


@pytest.mark.unit
def test_local_capacity_limit_rejects_unknown_limit_source() -> None:
    with pytest.raises(ValueError, match="unknown local capacity limit source"):
        local_capacity_limit(
            LocalCapacityConstraint(
                dimension="custom",
                reason_code="CUSTOM_CAPACITY",
                limit_source="custom",
            ),
            cpu_limit=8.0,
            memory_limit=24.0,
            dind_slots=1,
        )


@pytest.mark.unit
def test_local_capacity_blocker_classifies_deferred_and_unsatisfiable_requests() -> None:
    steady_cpu = LOCAL_CAPACITY_CONSTRAINTS[0]

    deferred = local_capacity_blocker(
        constraint=steady_cpu,
        limit=8.0,
        allocated=6.0,
        requested=3.0,
    )
    unsatisfiable = local_capacity_blocker(
        constraint=steady_cpu,
        limit=8.0,
        allocated=0.0,
        requested=9.0,
    )

    assert deferred is not None
    assert deferred.reason_code == "STEADY_CPU_CAPACITY_SATURATED"
    assert deferred.after == 9.0
    assert deferred.unsatisfiable is False
    assert unsatisfiable is not None
    assert unsatisfiable.after == 9.0
    assert unsatisfiable.unsatisfiable is True
    assert (
        local_capacity_blocker(
            constraint=steady_cpu,
            limit=8.0,
            allocated=2.0,
            requested=3.0,
        )
        is None
    )


@pytest.mark.unit
def test_local_capacity_module_does_not_export_incomplete_blocked_predicate() -> None:
    assert "local_capacity_blocked_condition" not in resource_capacity.__dict__


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
