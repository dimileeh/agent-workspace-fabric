"""Profile setup replay on PR monitor resume (#787, #1006).

Restart recovery, ``remonitor`` (failed -> monitoring_pr) and ``guide`` on a
monitor-origin block (blocked -> monitoring_pr) all resume through
``resume_pr_monitor_handoff``, which brings the runtime back with
``compose up``. When that recreated the agent container, this replays the
profile's ``setup``/``pre_agent`` phases before the monitor loop, its pre-push
validation, or any agent run continues. A failed replay parks the workspace in
``blocked`` with ``RUNTIME_SETUP_REPLAY_FAILED`` (#743) instead of failing it, so a
transient setup failure cannot kill a healthy monitor; the worker finalizes the
recovery from that reason code without a retry, and a later ``guide`` resumes.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import runtime_setup_replay as _replay
from awf.control.executor.constants import RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
from awf.control.executor.helpers import _profile_for_workspace
from awf.control.executor.quality_gates import _log
from awf.control.executor.state_ops import enter_blocked_for_monitor_setup_replay_failure
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import MergeCandidateRepository
from awf.node.git_manager import verify_head_object_exists
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError
from awf.runtime.ownership import EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME

MONITOR_RESUME_TRIGGER = "monitor_resume"
_SETUP_REPLAY_CLEANUP_FAILURE_STAGE = "runtime_setup_replay_cleanup_failure"


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
        # The restart (or a failed-but-usable one) can outlive the monitor lease;
        # fence so a superseded resume never reaches monitor build's failure paths.
        return bool(
            await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.monitoring_pr,
                action="resume_setup_unchanged",
                monitor_owner_id=monitor_owner_id,
            )
        )
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
            recover_missing_head=partial(
                _recover_pr_head_after_cleanup_failure,
                self,
                ws=ws,
                workspace_id=workspace_id,
                worktree_path=self._config.worktrees_root / workspace_id,
            ),
        )
    if outcome.ok:
        # Setup can outlive the monitor lease; fence again so a superseded resume
        # never reaches monitor build, whose failure paths would fail the row.
        return bool(
            await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.monitoring_pr,
                action="resume_setup_replayed",
                monitor_owner_id=monitor_owner_id,
            )
        )
    # Setup can outlive the monitor lease; fence again so a superseded resume
    # never parks the row underneath a newer claimant.
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_setup_replay_failed",
        monitor_owner_id=monitor_owner_id,
    ):
        return False
    await enter_blocked_for_monitor_setup_replay_failure(
        self._session_factory,
        workspace_id=workspace_id,
        message=f"monitor recovery: {outcome.message}",
        failure_reason=outcome.failure_reason or FailureReason.infrastructure_failure,
        details=outcome.details,
        monitor_owner_id=monitor_owner_id,
    )
    return False


async def _recover_pr_head_after_cleanup_failure(
    self: Any,
    exc: ComposeExecCleanupError,
    *,
    ws: Workspace,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """Recover a missing HEAD onto the PR's pushed head after a replay cleanup failure.

    The executor's filesystem recovery rebuilds the branch on the commit it is
    given. ``base_commit`` would flatten the pushed PR history, so anchor it on
    the open merge candidate head (else the last monitored commit) instead.
    """
    if await verify_head_object_exists(worktree_path):
        return True
    async with self._session_factory() as session:
        candidate = await MergeCandidateRepository(
            session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
    anchor = (candidate.head_sha if candidate is not None else None) or ws.monitor_last_commit_sha
    if anchor is None:
        _log.warning(
            "executor.resume_setup_replay_head_unrecoverable",
            workspace_id=workspace_id,
            reason_code=exc.reason_code,
        )
        return False
    return bool(
        await self._recover_missing_git_head_or_mark_failed(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=anchor,
            branch_name=ws.branch_name or f"awf/{workspace_id}",
            from_status=WorkspaceStatus.monitoring_pr,
            stage=_SETUP_REPLAY_CLEANUP_FAILURE_STAGE,
            error=exc,
            task_tag=ws.task_tag,
            mark_failed_on_failure=False,
        )
    )
