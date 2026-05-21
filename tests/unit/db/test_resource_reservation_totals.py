"""Resource reservation aggregate helpers."""

from __future__ import annotations

import pytest

from awf.db.repositories import empty_resource_reservation_totals


@pytest.mark.unit
def test_empty_resource_reservation_totals_covers_all_reservation_dimensions() -> None:
    assert empty_resource_reservation_totals() == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
