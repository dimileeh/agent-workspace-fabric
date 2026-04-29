"""Local reservation capacity math shared by scheduler and metrics views."""

from __future__ import annotations

from dataclasses import dataclass

from awf.common.config import Settings
from awf.service.disk import DiskCheck

_MIB = 1024 * 1024
type Number = int | float


@dataclass(frozen=True)
class WorkspaceResourceDefaults:
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float


@dataclass(frozen=True)
class ReservedResources:
    active_workspace_count: int
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int = 0
    dind_slots: int = 0


@dataclass(frozen=True)
class CapacityDimension:
    limit: Number | None
    reserved: Number
    available: Number | None
    available_after_next_default: Number | None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Number | str | None]:
        return {
            "limit": self.limit,
            "reserved": self.reserved,
            "available": self.available,
            "available_after_next_default": self.available_after_next_default,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ResourceCapacitySummary:
    steady_cpu: CapacityDimension
    peak_cpu: CapacityDimension
    steady_memory_gb: CapacityDimension
    peak_memory_gb: CapacityDimension
    disk_mb: CapacityDimension
    dind_slots: CapacityDimension
    pressure_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "steady_cpu": self.steady_cpu.to_dict(),
            "peak_cpu": self.peak_cpu.to_dict(),
            "steady_memory_gb": self.steady_memory_gb.to_dict(),
            "peak_memory_gb": self.peak_memory_gb.to_dict(),
            "disk_mb": self.disk_mb.to_dict(),
            "dind_slots": self.dind_slots.to_dict(),
            "pressure_reasons": list(self.pressure_reasons),
        }


def resource_capacity_summary(
    *,
    settings: Settings,
    reserved: ReservedResources,
    resource_defaults: WorkspaceResourceDefaults,
    disk_check: DiskCheck | None,
) -> ResourceCapacitySummary:
    steady_cpu = _bounded_dimension(
        limit=settings.local_capacity_cpu_cores,
        reserved=reserved.steady_cpu,
        next_default=resource_defaults.steady_cpu,
        unknown_reason="STEADY_CPU_CAPACITY_UNKNOWN",
        saturated_reason="STEADY_CPU_CAPACITY_SATURATED",
    )
    peak_cpu = _bounded_dimension(
        limit=settings.local_capacity_cpu_cores,
        reserved=reserved.peak_cpu,
        next_default=resource_defaults.peak_cpu,
        unknown_reason="PEAK_CPU_CAPACITY_UNKNOWN",
        saturated_reason="PEAK_CPU_CAPACITY_SATURATED",
    )
    steady_memory = _bounded_dimension(
        limit=settings.local_capacity_memory_gb,
        reserved=reserved.steady_memory_gb,
        next_default=resource_defaults.steady_memory_gb,
        unknown_reason="STEADY_MEMORY_CAPACITY_UNKNOWN",
        saturated_reason="STEADY_MEMORY_CAPACITY_SATURATED",
    )
    peak_memory = _bounded_dimension(
        limit=settings.local_capacity_memory_gb,
        reserved=reserved.peak_memory_gb,
        next_default=resource_defaults.peak_memory_gb,
        unknown_reason="PEAK_MEMORY_CAPACITY_UNKNOWN",
        saturated_reason="PEAK_MEMORY_CAPACITY_SATURATED",
    )
    disk = _disk_dimension(disk_check=disk_check, reserved_mb=reserved.disk_mb)
    dind = _bounded_dimension(
        limit=settings.local_capacity_dind_slots,
        reserved=reserved.dind_slots,
        next_default=0,
        unknown_reason="DIND_CAPACITY_UNKNOWN",
        saturated_reason="DIND_CAPACITY_SATURATED",
    )
    return ResourceCapacitySummary(
        steady_cpu=steady_cpu,
        peak_cpu=peak_cpu,
        steady_memory_gb=steady_memory,
        peak_memory_gb=peak_memory,
        disk_mb=disk,
        dind_slots=dind,
        pressure_reasons=tuple(
            reason
            for reason in (
                steady_cpu.reason_code,
                peak_cpu.reason_code,
                steady_memory.reason_code,
                peak_memory.reason_code,
                disk.reason_code,
                dind.reason_code,
            )
            if reason is not None
        ),
    )


def _bounded_dimension(
    *,
    limit: Number | None,
    reserved: Number,
    next_default: Number,
    unknown_reason: str,
    saturated_reason: str,
) -> CapacityDimension:
    if limit is None:
        return CapacityDimension(
            limit=None,
            reserved=reserved,
            available=None,
            available_after_next_default=None,
            reason_code=unknown_reason,
        )
    raw_available = limit - reserved
    available = _clamp_available(raw_available)
    return CapacityDimension(
        limit=limit,
        reserved=reserved,
        available=available,
        available_after_next_default=_clamp_available(raw_available - next_default),
        reason_code=saturated_reason if raw_available <= 0 else None,
    )


def _disk_dimension(*, disk_check: DiskCheck | None, reserved_mb: int) -> CapacityDimension:
    if disk_check is None:
        return CapacityDimension(
            limit=None,
            reserved=reserved_mb,
            available=None,
            available_after_next_default=None,
            reason_code="DISK_CAPACITY_UNKNOWN",
        )
    limit = disk_check.free_bytes // _MIB
    raw_available = limit - reserved_mb
    return CapacityDimension(
        limit=limit,
        reserved=reserved_mb,
        available=max(0, raw_available),
        available_after_next_default=max(0, raw_available),
        reason_code=_disk_reason_code(disk_check=disk_check, raw_available=raw_available),
    )


def _disk_reason_code(*, disk_check: DiskCheck, raw_available: int) -> str | None:
    if not disk_check.ok:
        return disk_check.reason
    if raw_available <= 0:
        return "DISK_RESERVATION_PRESSURE"
    return None


def _clamp_available(value: Number) -> Number:
    if isinstance(value, int):
        return max(0, value)
    return max(0.0, value)
