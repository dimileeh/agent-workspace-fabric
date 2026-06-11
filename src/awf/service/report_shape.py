"""Shared report-shape helpers for the smoke and profile-doctor reports.

Both ``awf smoke`` (``service/smoke.py``) and ``awf profile doctor``
(``service/profile_doctor.py``) emit the same phase-list report shape
(``name``/``status``/``reason_code``/``message``/``evidence``/``action`` per
phase, plus a top-level ``status``/``next_actions``). These two pure utilities
collapse the per-phase statuses into the overall status and gather the
actionable hints. They live here — rather than as private helpers of one report
producer imported by the other — so neither report module depends on the
internals of the other.
"""

from __future__ import annotations

from typing import Any

# Sentinel ``action`` value meaning "nothing to do" for a report phase. Shared by
# every report producer (smoke, profile doctor) and consumer (CLI renderers) so the
# no-op string lives in exactly one place rather than being re-typed per module.
NO_ACTION = "No action required."


def compute_overall_status(phase_statuses: list[str]) -> str:
    """Collapse per-phase status values into the overall report status."""
    has_fail = any(s == "fail" for s in phase_statuses)
    has_warn = any(s == "warn" for s in phase_statuses)
    if has_fail:
        return "fail"
    if has_warn:
        return "warn"
    return "ok"


def collect_next_actions(phases: list[dict[str, Any]]) -> list[str]:
    """Collect non-no-op actionable hints from report phases."""
    actions: list[str] = []
    for phase in phases:
        action = phase.get("action", "")
        if action and action != NO_ACTION:
            actions.append(action)
    return actions
