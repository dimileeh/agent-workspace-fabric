"""Profile setup replay on PR monitor resume (#787, #1006).

Restart recovery, ``remonitor`` (failed -> monitoring_pr) and ``guide`` on a
monitor-origin block (blocked -> monitoring_pr) all resume through
``resume_pr_monitor_handoff``, which brings the runtime back with
``compose up``. When that recreated the agent container, this replays the
profile's ``setup``/``pre_agent`` phases before the monitor loop, its pre-push
validation, or any agent run continues. A failed replay fails the workspace with
``RUNTIME_SETUP_REPLAY_FAILED``, like the other ``MONITOR_RECOVERY_*`` resume
failures, so the worker finalizes the recovery without a retry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.control.executor import runtime_setup_replay as _replay
from awf.control.executor.constants import RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
from awf.control.executor.helpers import _profile_for_workspace
from awf.control.executor.quality_gates import _log
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError
from awf.runtime.ownership import EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME

MONITOR_RESUME_TRIGGER = "monitor_resume"


async def replay_setup_on_monitor_resume(
    self: Any,
    *,
    ws: Workspace,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    profile: WorkspaceProfile | None,
    identity_before: _replay.AgentIdentityObservation,
    monitor_owner_id: str | None,
) -> bool:
    """Return ``True`` to continue the resume; ``False`` when it must stop."""
    identity_after = await _replay.capture_agent_container_identity(compose_project)
    verdict = _replay.runtime_recreation_decision(identity_before, identity_after)
    if verdict.decision is _replay.RecreationDecision.unchanged:
        return True
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_setup_replay",
        monitor_owner_id=monitor_owner_id,
    ):
        return False
    if profile is None:
        # The earlier resolve+sync failed (and was logged); resolve once more the
        # way the monitor build does before giving up on the replay.
        try:
            profile = _profile_for_workspace(
                ws,
                worktree_path=self._config.worktrees_root / workspace_id,
                planning_max_iterations_default=self._config.planning_max_iterations_default,
            )
        except (OSError, ValueError, ProfileResolutionError):
            _log.exception(
                "executor.resume_setup_replay_profile_unavailable", workspace_id=workspace_id
            )
    if profile is None:
        outcome = _replay.SetupReplayOutcome(
            ok=False,
            replayed=False,
            reason_code=RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
            failure_reason=FailureReason.profile_resolution_failure,
            message="runtime setup replay failed: workspace profile could not be resolved",
            details={"trigger": MONITOR_RESUME_TRIGGER, "basis": verdict.basis},
        )
    else:
        outcome = await _replay.replay_setup_if_recreated(
            self,
            session_factory=self._session_factory,
            validation=self._validation,
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            worktree_path=self._config.worktrees_root / workspace_id,
            before=identity_before,
            after=identity_after,
            trigger=MONITOR_RESUME_TRIGGER,
            ownership_event_name=EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        )
    if outcome.ok:
        return True
    # Setup can outlive the monitor lease; fence again so a superseded resume
    # never fails the row underneath a newer claimant.
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_setup_replay_failed",
        monitor_owner_id=monitor_owner_id,
    ):
        return False
    await self._mark_failed(
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.monitoring_pr,
        failure_reason=outcome.failure_reason or FailureReason.infrastructure_failure,
        message=f"monitor recovery: {outcome.message}"[:2000],
        reason_code=outcome.reason_code,
        details=outcome.details,
        monitor_owner_id=monitor_owner_id,
    )
    return False
