"""Operator-facing doctor reason text for monitor-resume runtime restarts (#936).

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_monitor_runtime_restart_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the monitor-resume runtime restart catalog reason text entries."""
    return {
        "MONITOR_RUNTIME_RESTART_EXHAUSTED": reason_text_cls(
            (
                "AWF could not bring the workspace runtime back after several consecutive "
                "PR monitor resumes, so it stopped retrying and parked the workspace in "
                "blocked with its PR, worktree and stack preserved."
            ),
            (
                "Read the last compose error and the failure count in the "
                "workspace.monitor_runtime_restart_exhausted event or workspace logs, and fix "
                "the stack (for example a dependency that cannot finish its recovery, a "
                "missing companion secret or a full disk). Then run guide on the blocked "
                "workspace to resume monitoring; the resume gets a fresh set of attempts."
            ),
            (
                "Every monitor resume (restart recovery, remonitor or guide) restarts the "
                "agent container and brings dependencies up. That restart failed on several "
                "resumes in a row, and retrying it forever would hide the problem."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("MONITOR_RUNTIME_RESTART_EXHAUSTED"),
        ),
        "MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT": reason_text_cls(
            (
                "A PR monitor resume waited for a running dependency service to become "
                "healthy and the stack's startup budget ran out."
            ),
            (
                "Check the named dependency's logs (for example Postgres crash recovery "
                "after an unclean stop). If its recovery legitimately takes longer, raise "
                "docker.startup_timeout_seconds in the workspace profile. AWF retries the "
                "resume and parks the workspace in blocked after repeated failures."
            ),
            (
                "Monitor resume never restarts a dependency that is still starting or "
                "unhealthy; it waits for it instead. The dependency stayed unsettled for "
                "longer than docker.startup_timeout_seconds (or a companion's larger "
                "compose_up_timeout_seconds)."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT"),
        ),
        "COMPANION_SPECS_UNRESOLVED": reason_text_cls(
            (
                "A PR monitor resume could not resolve the workspace's companion service "
                "specs, so it refused to restart the runtime."
            ),
            (
                "Read the executor.resume_companion_spec_resolution_failed log for the "
                "parse error and fix the companion entries in the workspace task_policy. "
                "AWF retries the resume and parks the workspace in blocked after repeated "
                "failures; run guide on the blocked workspace once the policy is fixed."
            ),
            (
                "The stored companion policy could not be parsed. Only the specs say which "
                "companions carry env secrets that the restart must recreate, so restarting "
                "without them could leave a rotated or revoked secret in a running companion."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("COMPANION_SPECS_UNRESOLVED"),
        ),
    }
