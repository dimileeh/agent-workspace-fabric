"""Profile setup replay after the PR monitor recreates the agent runtime (#787).

``_restart_monitor_agent_service_or_fail`` recovers an unhealthy agent service
with a whole-project ``compose up --force-recreate``. When that recreated the
agent container, setup state inside it (for example a tool under ``/tmp``) is
gone, so ``setup``/``pre_agent`` are replayed before the agent retry or the
monitor's pre-push validation needs them. A failed replay terminates the monitor
with ``RUNTIME_SETUP_REPLAY_FAILED``, mirroring
``_terminate_monitor_for_unhealthy_agent_service``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from awf.control.executor import runtime_setup_replay as _replay
from awf.control.executor.constants import RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
from awf.control.executor.helpers import _profile_for_workspace
from awf.db.enums import FailureReason
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError
from awf.runtime.ownership import MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import _MonitorAgentServiceRecoveryFailedError

MONITOR_AGENT_SERVICE_RECOVERY_TRIGGER = "monitor_agent_service_recovery"


async def replay_setup_after_monitor_agent_service_restart(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    identity_before: _replay.AgentIdentityObservation,
    recovery_details: dict[str, object],
) -> None:
    """Replay setup when the restart recreated the agent; raise on a failed replay."""
    identity_after = await _replay.capture_agent_container_identity(compose_project)
    verdict = _replay.runtime_recreation_decision(identity_before, identity_after)
    if verdict.decision is _replay.RecreationDecision.unchanged:
        return
    validation = self._deps.validation
    if validation is None:
        # The recreated runtime lost its setup state and nothing here can replay
        # it, so fail closed rather than retry the agent without setup.
        await _terminate_failed_replay(
            self,
            workspace_id=workspace_id,
            outcome=_replay.SetupReplayOutcome(
                ok=False,
                replayed=False,
                reason_code=RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
                failure_reason=FailureReason.infrastructure_failure,
                message="runtime setup replay failed: no validation runner configured",
                details={"trigger": MONITOR_AGENT_SERVICE_RECOVERY_TRIGGER, "basis": verdict.basis},
            ),
            recovery_details=recovery_details,
        )
    worktree_path = self._worktrees_root / workspace_id
    profile: WorkspaceProfile | None = None
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - superseded check ran just before
            return
        try:
            profile = _profile_for_workspace(workspace, worktree_path=worktree_path)
        except (OSError, ValueError, ProfileResolutionError):
            _log.exception(
                "monitor.agent_service_setup_replay_profile_unavailable",
                workspace_id=workspace_id,
            )
    if profile is None:
        outcome = _replay.SetupReplayOutcome(
            ok=False,
            replayed=False,
            reason_code=RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
            failure_reason=FailureReason.profile_resolution_failure,
            message="runtime setup replay failed: workspace profile could not be resolved",
            details={"trigger": MONITOR_AGENT_SERVICE_RECOVERY_TRIGGER, "basis": verdict.basis},
        )
    else:
        outcome = await _replay.replay_setup_if_recreated(
            self,
            session_factory=self._deps.session_factory,
            validation=validation,
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            worktree_path=worktree_path,
            before=identity_before,
            after=identity_after,
            trigger=MONITOR_AGENT_SERVICE_RECOVERY_TRIGGER,
            ownership_event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        )
    if outcome.ok:
        return
    await _terminate_failed_replay(
        self,
        workspace_id=workspace_id,
        outcome=outcome,
        recovery_details=recovery_details,
    )


async def _terminate_failed_replay(
    self: Any,
    *,
    workspace_id: str,
    outcome: _replay.SetupReplayOutcome,
    recovery_details: dict[str, object],
) -> NoReturn:
    message = outcome.message or "runtime setup replay failed"
    details = dict(outcome.details or {})
    await self._terminate_failed(
        workspace_id,
        message=message,
        reason_code=outcome.reason_code,
        details={**details, "agent_service_recovery": recovery_details},
        failure_reason=outcome.failure_reason or FailureReason.infrastructure_failure,
    )
    # Monitor operation handlers persist exc.details as the agent-service recovery
    # payload, so carry the recovery provenance alongside the replay evidence.
    raise _MonitorAgentServiceRecoveryFailedError(
        message,
        reason_code=outcome.reason_code,
        details={**details, **recovery_details},
    )
