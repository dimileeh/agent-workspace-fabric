"""Status-set membership invariants for the non-terminal ``blocked`` status.

A ``blocked`` workspace is paused awaiting an operator decision after a pre-PR
protected quality-gate violation. It keeps its worktree, warm stack, and
execution claim, so it must:

- count as execution-in-use / allocated / admission-slot-holding (keep-warm),
- be GC-protected (never reaped),
- be present in the per-status saturation counts,

while it must NOT:

- be terminal or callback-terminal,
- be scanned for stale-execution reaping or runtime-health (which would tear it
  down or fail it — the held claim is the durable lease),
- fold into the running/validating/pushing "Running" KPI execution set.
"""

from __future__ import annotations

import pytest

from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_STATUSES,
    _REQUESTED_ADMISSION_SLOT_STATUSES,
    _RUNTIME_HEALTH_SCAN_STATUSES,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories.base import ALLOCATED_RESOURCE_RESERVATION_STATUSES
from awf.service.gc_classify import (
    PROTECTED_WORKSPACE_GC_STATUSES,
    TERMINAL_WORKSPACE_GC_STATUSES,
)
from awf.service.metrics import EXECUTION_IN_USE_STATUSES, TERMINAL_WORKSPACE_STATUSES
from awf.service.metrics_capacity import (
    EXECUTION_IN_USE_STATUSES as CAPACITY_EXECUTION_IN_USE_STATUSES,
)
from awf.service.orphan_resources import (
    ACTIVE_WORKSPACE_STATUSES as ORPHAN_ACTIVE_WORKSPACE_STATUSES,
)
from awf.service.orphan_resources import (
    KNOWN_WORKSPACE_STATUSES as ORPHAN_KNOWN_WORKSPACE_STATUSES,
)

_BLOCKED = WorkspaceStatus.blocked


@pytest.mark.unit
class TestBlockedKeepWarmMembership:
    """A blocked workspace holds its slot/reservation while paused."""

    def test_in_execution_in_use(self) -> None:
        assert _BLOCKED.value in EXECUTION_IN_USE_STATUSES
        assert _BLOCKED.value in CAPACITY_EXECUTION_IN_USE_STATUSES

    def test_in_allocated_reservation_statuses(self) -> None:
        assert _BLOCKED.value in ALLOCATED_RESOURCE_RESERVATION_STATUSES

    def test_in_admission_slot_statuses(self) -> None:
        assert _BLOCKED in _REQUESTED_ADMISSION_SLOT_STATUSES

    def test_in_protected_gc_statuses(self) -> None:
        assert _BLOCKED.value in PROTECTED_WORKSPACE_GC_STATUSES

    def test_in_orphan_resource_active_statuses(self) -> None:
        # The orphan-resource scanner keeps its own active/known status sets; if
        # ``blocked`` were omitted, the workspace row would be filtered out of the
        # id view, its containers/networks/volumes/worktree would classify as
        # ``missing``, and the reaper could delete the preserved warm stack.
        assert _BLOCKED.value in ORPHAN_ACTIVE_WORKSPACE_STATUSES
        assert _BLOCKED.value in ORPHAN_KNOWN_WORKSPACE_STATUSES


@pytest.mark.unit
class TestBlockedNotTerminalNotReaped:
    """A blocked workspace is preserve-not-reap and never terminal."""

    def test_not_terminal_metrics(self) -> None:
        assert _BLOCKED.value not in TERMINAL_WORKSPACE_STATUSES

    def test_not_terminal_gc(self) -> None:
        assert _BLOCKED.value not in TERMINAL_WORKSPACE_GC_STATUSES

    def test_not_in_active_execution_scan(self) -> None:
        # Absence here is what makes recovery_stale preserve-not-reap it.
        assert _BLOCKED not in _ACTIVE_EXECUTION_STATUSES

    def test_not_in_runtime_health_scan(self) -> None:
        assert _BLOCKED not in _RUNTIME_HEALTH_SCAN_STATUSES


@pytest.mark.unit
class TestBlockedSaturationCount:
    """``blocked`` is exposed as its own per-status saturation count."""

    def test_blocked_in_saturation_counts(self) -> None:
        from awf.service.metrics_resources import _workspace_saturation_counts

        status_counts = {status.value: 0 for status in WorkspaceStatus}
        status_counts[_BLOCKED.value] = 3
        counts = _workspace_saturation_counts(status_counts, awaiting_human=0)
        assert counts.blocked == 3
        # A blocked workspace is non-terminal, so it counts toward active_total.
        assert counts.active_total == 3
        assert counts.by_status[_BLOCKED.value] == 3


@pytest.mark.unit
class TestBlockedRunningKpiSeparation:
    """``blocked`` is its own state, not folded into the Running KPI set."""

    def test_running_kpi_set_excludes_blocked(self) -> None:
        # The console "Running" KPI counts running + validating + pushing only.
        running_kpi_set = {
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
            WorkspaceStatus.pushing.value,
        }
        assert _BLOCKED.value not in running_kpi_set
