"""Planning-scope failure helpers for executor planning flow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from awf.control.executor.types import _PlanningRunFailure
from awf.runtime.planning import AGENT_PLAN_PHASE_SCOPE_VIOLATION


def _build_planning_scope_failure(
    *,
    scope_phase: str,
    required_paths: Sequence[Path],
    offending_paths: Sequence[Path],
    summary: str,
    offending_commands: Sequence[str] = (),
    near_miss_plan_artifacts: Sequence[Mapping[str, object]] = (),
) -> _PlanningRunFailure:
    required = [path.as_posix() for path in required_paths]
    offending = [path.as_posix() for path in sorted(offending_paths)]
    commands = [command for command in offending_commands if command]
    recommended_action = (
        "Retry planning from a clean workspace. Discard the premature implementation "
        "by default, and salvage the preserved branch only after explicit operator approval."
    )
    artifact = required[0] if required else "the configured plan artifact"
    if offending:
        message = f"{summary}: {', '.join(offending[:10])}. {recommended_action}"
    else:
        message = f"{summary}. {recommended_action}"
    planning_scope: dict[str, object] = {
        "scope_phase": scope_phase,
        "required_paths": required,
        "offending_paths": offending,
        "offending_commands": commands,
        "recommended_action": recommended_action,
        "recovery_strategy": "discard_and_replan",
        "salvage_policy": "explicit_salvage_required",
        "plan_artifact": artifact,
        "near_miss_plan_artifacts": [dict(item) for item in near_miss_plan_artifacts],
    }
    return _PlanningRunFailure(
        message=message,
        reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        details={
            "planning_scope": planning_scope,
            "near_miss_plan_artifacts": planning_scope["near_miss_plan_artifacts"],
            "recommended_action": recommended_action,
            "recovery_strategy": "discard_and_replan",
            "salvage_policy": "explicit_salvage_required",
            # Temporary compatibility for older console code that read `scope`.
            "scope": {
                "scope_phase": scope_phase,
                "required_paths": required,
                "forbidden_paths": offending,
                "recommended_action": recommended_action,
            },
        },
    )
