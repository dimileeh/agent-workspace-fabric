"""Status-set membership invariants for the non-terminal ``recovering`` status.

A ``recovering`` workspace is paused mid-run after a *transient provider failure*
(e.g. an idle-timeout) while it auto-heals across the provider cooldown. Unlike
``blocked`` (which awaits an operator decision), ``recovering`` resumes itself —
but it is the **same KIND** of pause: non-terminal, keeps its worktree + warm
stack + execution claim. So it gets the same status accounting as ``blocked``.

The rule this test pins (#612 slice 1): ``recovering`` is a member of every
status set that already contains ``blocked``, and is absent from every set
``blocked`` is deliberately absent from (stuck-detection / terminal /
preserve-not-reap scans). It must:

- count as execution-in-use / allocated / admission-slot-holding (keep-warm),
- be GC-protected and orphan-active (never reaped — the warm stack is the point),
- hold its owned paths + host ports (overlap/port-conflict),
- surface in the advisory overlap graph as a "running" node,
- be present in the per-status saturation counts and ``active_total``,

while it must NOT:

- be terminal,
- be scanned for stale-execution reaping or runtime-health (which would tear it
  down or fail it — the held claim is the durable lease),
- count toward SLO stuck-detection (an intentional auto-retry pause is not stuck),
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
from awf.db.repositories.base import (
    ACTIVE_OWNED_PATH_OVERLAP_STATUSES,
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    HOST_PORT_CONFLICT_STATUSES,
)
from awf.service.gc_classify import (
    PROTECTED_WORKSPACE_GC_STATUSES,
    TERMINAL_WORKSPACE_GC_STATUSES,
)
from awf.service.metrics import EXECUTION_IN_USE_STATUSES, TERMINAL_WORKSPACE_STATUSES
from awf.service.metrics_capacity import (
    EXECUTION_IN_USE_STATUSES as CAPACITY_EXECUTION_IN_USE_STATUSES,
)
from awf.service.metrics_slo import (
    EXECUTION_IN_USE_STATUSES as SLO_EXECUTION_IN_USE_STATUSES,
)
from awf.service.orphan_resources import (
    ACTIVE_WORKSPACE_STATUSES as ORPHAN_ACTIVE_WORKSPACE_STATUSES,
)
from awf.service.orphan_resources import (
    KNOWN_WORKSPACE_STATUSES as ORPHAN_KNOWN_WORKSPACE_STATUSES,
)
from awf.service.overlap_graph import (
    ACTIVE_OVERLAP_GRAPH_STATUSES,
    RUNNING_WORKSPACE_STATUSES,
    _queue_state_for_status,
)

_RECOVERING = WorkspaceStatus.recovering


@pytest.mark.unit
class TestRecoveringKeepWarmMembership:
    """A recovering workspace holds its slot/reservation/stack while paused."""

    def test_in_execution_in_use(self) -> None:
        assert _RECOVERING.value in EXECUTION_IN_USE_STATUSES
        assert _RECOVERING.value in CAPACITY_EXECUTION_IN_USE_STATUSES

    def test_in_allocated_reservation_statuses(self) -> None:
        assert _RECOVERING.value in ALLOCATED_RESOURCE_RESERVATION_STATUSES

    def test_in_admission_slot_statuses(self) -> None:
        assert _RECOVERING in _REQUESTED_ADMISSION_SLOT_STATUSES

    def test_in_protected_gc_statuses(self) -> None:
        assert _RECOVERING.value in PROTECTED_WORKSPACE_GC_STATUSES

    def test_in_orphan_resource_active_statuses(self) -> None:
        # The orphan-resource scanner keeps its own active/known status sets; if
        # ``recovering`` were omitted, the workspace row would be filtered out of
        # the id view, its containers/networks/volumes/worktree would classify as
        # ``missing``, and the reaper could delete the preserved warm stack.
        assert _RECOVERING.value in ORPHAN_ACTIVE_WORKSPACE_STATUSES
        assert _RECOVERING.value in ORPHAN_KNOWN_WORKSPACE_STATUSES

    def test_in_owned_path_and_host_port_statuses(self) -> None:
        # It keeps its worktree (owned paths) and warm compose stack (host ports)
        # while paused, so a concurrent overlapping create/retry must still treat
        # both as occupied.
        assert _RECOVERING.value in ACTIVE_OWNED_PATH_OVERLAP_STATUSES
        assert _RECOVERING.value in HOST_PORT_CONFLICT_STATUSES

    def test_in_overlap_graph_running_statuses(self) -> None:
        assert _RECOVERING.value in RUNNING_WORKSPACE_STATUSES
        assert _RECOVERING.value in ACTIVE_OVERLAP_GRAPH_STATUSES

    def test_overlap_graph_queue_state_is_running(self) -> None:
        assert _queue_state_for_status(_RECOVERING.value) == "running"


@pytest.mark.unit
class TestRecoveringNotTerminalNotReaped:
    """A recovering workspace is preserve-not-reap and never terminal."""

    def test_not_terminal_metrics(self) -> None:
        assert _RECOVERING.value not in TERMINAL_WORKSPACE_STATUSES

    def test_not_terminal_gc(self) -> None:
        assert _RECOVERING.value not in TERMINAL_WORKSPACE_GC_STATUSES

    def test_not_in_active_execution_scan(self) -> None:
        # Absence here is what makes recovery_stale preserve-not-reap it.
        assert _RECOVERING not in _ACTIVE_EXECUTION_STATUSES

    def test_not_in_runtime_health_scan(self) -> None:
        assert _RECOVERING not in _RUNTIME_HEALTH_SCAN_STATUSES

    def test_not_in_slo_execution_in_use(self) -> None:
        # The SLO stuck-detection execution set deliberately excludes intentional
        # pauses (it omits ``blocked`` too): an auto-retry pause is not "stuck".
        assert _RECOVERING.value not in SLO_EXECUTION_IN_USE_STATUSES


@pytest.mark.unit
class TestRecoveringSaturationCount:
    """``recovering`` is exposed as its own per-status saturation count."""

    def test_recovering_in_saturation_counts(self) -> None:
        from awf.service.metrics_resources import _workspace_saturation_counts

        status_counts = {status.value: 0 for status in WorkspaceStatus}
        status_counts[_RECOVERING.value] = 3
        counts = _workspace_saturation_counts(status_counts, awaiting_human=0)
        assert counts.recovering == 3
        # A recovering workspace is non-terminal, so it counts toward active_total.
        assert counts.active_total == 3
        assert counts.by_status[_RECOVERING.value] == 3


@pytest.mark.unit
class TestRecoveringRunningKpiSeparation:
    """``recovering`` is its own state, not folded into the Running KPI set."""

    def test_running_kpi_set_excludes_recovering(self) -> None:
        # The console "Running" KPI counts running + validating + pushing only.
        running_kpi_set = {
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
            WorkspaceStatus.pushing.value,
        }
        assert _RECOVERING.value not in running_kpi_set
