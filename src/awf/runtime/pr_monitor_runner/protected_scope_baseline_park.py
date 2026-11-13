"""Blocked park for a transiently unavailable protected-scope baseline (#1221).

When the PR-branch fetch behind protected-scope validation keeps failing on a
transient network/DNS/TLS fault after its bounded retries, the monitor still fails
closed (nothing is pushed), but it must not terminally fail the workspace: terminal
cleanup would remove the worktree together with the unpushed repair commit. This
module parks the workspace ``monitoring_pr -> blocked`` instead, keeping the
worktree and commit, with a ``monitor_*`` resume phase so a later ``guide``
resumes the monitor, re-runs protected-scope validation and pushes the commit.

Attached to ``PullRequestMonitorRunner`` via the runner mixin, like the other
focused runner modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from awf.common.audit import redact_audit_text
from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    enter_blocked_for_protected_violation_in_session,
)
from awf.control.quality_gates import PROTECTED_QUALITY_GATE_BLOCK_TYPE
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.runtime.pr_monitor_runner.constants import _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError


async def _protected_scope_baseline_park_resume_pending(self: Any, workspace_id: str) -> bool:
    """Return True while a baseline-outage park's kept commit is still unsettled.

    The park is the only block recording ``PROTECTED_SCOPE_DIFF_UNAVAILABLE`` as its
    ``block_reason_code``, and ``block_resume_phase`` stays set until a resume is
    finalized (the kept commit published, or explicitly dropped by a directive). The
    two columns are written in the block-transition commit itself, so this durable
    marker survives restarts and the loop's later ``_persist_state``. The operator-hint
    resume uses it to suppress the non-fast-forward ``reset --hard`` resync, which
    would otherwise drop the commit this park promised to keep.
    """
    async with self._deps.session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    return (
        ws is not None
        and ws.block_reason_code == _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON
        and ws.block_resume_phase is not None
    )


async def _park_for_protected_scope_baseline_unavailable(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    resume_phase: str,
    message: str,
    details: Mapping[str, object] | None = None,
    pr_number: int | None = None,
    repo: RepoRef | None = None,
) -> _GitPushResult:
    """Park the workspace in ``blocked`` with the worktree and unpushed commit kept.

    Unlike ``_pause_monitor_for_protected_scope_block`` this records NO
    ``_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY`` marker: that marker means "an
    ungranted protected commit is preserved", and the directive-resume leak guard
    would then refuse to push this legitimate repair commit. The preserved HEAD is
    recorded in the transition/event payload for the operator; the resume keeps the
    commit through ``_protected_scope_baseline_park_resume_pending``.

    ``pr_number`` re-arms the #910 terminal-PR guard: the bounded fetch retries can
    outlast the seam's own check, and a moot PR must not be parked. A lost CAS (the
    row left ``monitoring_pr`` or a newer monitor owns it) returns the plain failed
    result without touching the row, mirroring the protected pause.
    """
    if pr_number is not None:
        moot_result = await self._post_action_pr_terminal_push_result_if_moot(
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="protected_scope_baseline_park",
            repo=repo,
            worktree_path=worktree_path,
        )
        if moot_result is not None:
            return cast(_GitPushResult, moot_result)
    preserved_head_sha = await self._rev_parse_head(worktree_path)
    async with self._deps.session_factory() as session:
        ws = await enter_blocked_for_protected_violation_in_session(
            session,
            WorkspaceRepository(session),
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            violations=(),
            resume_phase=resume_phase,
            block_type=PROTECTED_QUALITY_GATE_BLOCK_TYPE,
            block_reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
            transition_reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
            monitor_owner_id=getattr(self, "_monitor_owner_id", None),
            fence_unclaimed_monitor=True,
            extra_payload={
                "preserved_head_sha": preserved_head_sha,
                "remote_branch": remote_branch,
                "transient": True,
            },
        )
        if ws is None:
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=message,
                reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
            )
        block_epoch = ws.block_epoch
        await session.commit()

    _log.warning(
        "monitor.protected_scope_diff_unavailable_parked",
        workspace_id=workspace_id,
        reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
        preserved_head_sha=preserved_head_sha,
        block_epoch=block_epoch,
        resume_phase=resume_phase,
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.monitor_protected_scope_diff_unavailable_parked",
                reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                payload={
                    "preserved_head_sha": preserved_head_sha,
                    "block_epoch": block_epoch,
                    "resume_phase": resume_phase,
                    "remote_branch": remote_branch,
                    "message": redact_audit_text(message, limit=1000),
                    "pushed": False,
                },
            )
        ],
    )
    park_details: dict[str, object] = {"phase": "pre_push_committed_diff"}
    if details:
        park_details["pre_push_validation"] = dict(details)
    park_details.update(
        {
            "preserved_head_sha": preserved_head_sha,
            "block_epoch": block_epoch,
            "paused_into_blocked": True,
            "transient_baseline_unavailable": True,
            "pushed": False,
        }
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            f"{message}\n"
            "AWF parked the workspace in blocked and PRESERVED the unpushed commit; "
            "no push was attempted. Resume with guide once the PR branch is reachable."
        ),
        reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
        details=park_details,
        paused_into_blocked=True,
    )


async def _protected_scope_diff_unavailable_push_result(
    self: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    exc: ProtectedScopeDiffError,
    pr_number: int | None = None,
    repo: RepoRef | None = None,
    resume_phase: str = MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
) -> _GitPushResult:
    block = await self._protected_scope_diff_unavailable_block(
        workspace_id=workspace_id,
        remote_branch=remote_branch,
        exc=exc,
    )
    if block.transient_baseline_unavailable:  # commit-sink baseline outage: park (#1221)
        return cast(
            _GitPushResult,
            await self._park_for_protected_scope_baseline_unavailable(
                workspace_id=workspace_id,
                worktree_path=self._worktrees_root / workspace_id,
                remote_branch=remote_branch,
                resume_phase=resume_phase,
                message=block.message,
                pr_number=pr_number,
                repo=repo,
            ),
        )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=block.message,
        reason_code=block.reason_code,
    )
