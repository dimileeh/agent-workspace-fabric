"""Operator-facing doctor reason text for the runtime setup replay (#787, #1006).

Kept beside ``reasons`` so that module stays within the first-party file-line
guardrail; the entries are merged into ``_REASON_TEXT`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.service.doctor.reasons_helpers import reason_catalog_link

if TYPE_CHECKING:
    from awf.service.doctor.reasons import _ReasonText


def get_runtime_setup_replay_reasons(
    reason_text_cls: type[_ReasonText],
) -> dict[str, _ReasonText]:
    """Return the runtime setup replay catalog reason text entries."""
    return {
        "RUNTIME_SETUP_REPLAY_FAILED": reason_text_cls(
            (
                "AWF recreated the workspace agent runtime and could not re-run the profile's "
                "setup phase, so it stopped the workspace instead of validating without its "
                "tools."
            ),
            (
                "Read the failing setup command and its output in the block or failure "
                "details or workspace logs, and fix the profile setup or the dependency it "
                "needs. A PR-monitor workspace is parked in blocked with its work preserved: "
                "run guide to resume monitoring, which replays setup. A pre-PR workspace "
                "failed: retry it."
            ),
            (
                "A monitor resume (restart recovery, remonitor or guide) or an agent-service "
                "recovery created a new agent container. Setup state inside the old container, "
                "such as tools installed under /tmp, was gone, and replaying the profile's "
                "setup and pre_agent phases failed."
            ),
            "awf workspace logs <workspace_id>",
            reason_catalog_link("RUNTIME_SETUP_REPLAY_FAILED"),
        ),
    }
