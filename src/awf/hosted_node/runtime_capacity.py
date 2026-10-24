"""Validate hosted capacity; scheduling remains the ordinary resource broker."""

import math

from awf.hosted_node.config import RuntimeCapacity
from awf.hosted_node.identity import MaterialDeniedError
from awf.service.config import ServiceSettings
from awf.service.resource_capacity import LocalCapacityLimits


def _positive(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def validate_capacity(
    settings: ServiceSettings,
    headroom: RuntimeCapacity | None,
    detected: LocalCapacityLimits,
) -> None:
    capacities = (settings.local_capacity_cpu_cores, settings.local_capacity_memory_gb)
    steady = (settings.workspace_steady_cpu, settings.workspace_steady_memory_gb)
    peak = (settings.workspace_peak_cpu, settings.workspace_peak_memory_gb)
    measured = (detected.cpu_cores, detected.memory_gb)
    concurrency = (
        settings.worker_max_concurrent_provisions,
        settings.worker_max_concurrent_executions,
    )
    if headroom is None or not all(
        _positive(value) for value in (*capacities, *steady, *peak, *measured, *concurrency)
    ):
        raise MaterialDeniedError("HOSTED_CAPACITY_REQUIRED", 503)
    reserve = (headroom.cpu_headroom_cores, headroom.memory_headroom_gb)
    for capacity, low, high, actual, overhead in zip(
        capacities, steady, peak, measured, reserve, strict=True
    ):
        assert capacity is not None and actual is not None
        if low > high or max(concurrency) * high > capacity:
            raise MaterialDeniedError("HOSTED_CAPACITY_OVERCOMMITTED", 503)
        if capacity + overhead > actual:
            raise MaterialDeniedError("HOSTED_CAPACITY_HEADROOM_UNAVAILABLE", 503)
