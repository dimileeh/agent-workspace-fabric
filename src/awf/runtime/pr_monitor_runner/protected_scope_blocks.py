"""Protected-scope push-block terminals for the PR monitor.

Extracted from ``remote_repair`` so that module stays under the maintainability
line limit; behavior is unchanged. These methods turn a detected protected-scope
violation (or an unavailable diff baseline) into a push block / push result, and
perform the ``monitoring_pr -> blocked`` pause CAS. They are dispatched as runner
methods via the mixin, so cross-references go through ``self.``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from awf.common.audit import redact_audit_text
from awf.control.protected_block import (
    apply_protected_block_columns,
    load_active_operator_grant_specs,
)
from awf.control.quality_gates import (
    PROTECTED_VIOLATION_BLOCKED_REASON_CODE,
    QualityGateViolation,
    quality_gate_violation_details,
    quality_gate_violation_message,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    WorkspaceEventCreate,
    WorkspaceRepository,
)
from awf.runtime.pr_monitor_runner.constants import (
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
    MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _quality_gate_violation_paths,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)


async def _protected_scope_diff_unavailable_block(
    self: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    exc: ProtectedScopeDiffError,
) -> _ProtectedScopePushBlock:
    redacted_error = redact_audit_text(str(exc), limit=1000)
    message = (
        "AWF could not verify protected-scope changes before push; "
        "refusing to push this repair until the PR branch diff baseline "
        f"is available. {redacted_error}"
    )
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.monitor_protected_scope_push_blocked",
                reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
                payload={
                    "reason": "diff_baseline_unavailable",
                    "remote_branch": remote_branch,
                    "error": redacted_error,
                },
            )
        ],
    )
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    )


async def _protected_scope_diff_unavailable_push_result(
    self: Any,
    *,
    workspace_id: str,
    remote_branch: str,
    exc: ProtectedScopeDiffError,
) -> _GitPushResult:
    block = await self._protected_scope_diff_unavailable_block(
        workspace_id=workspace_id,
        remote_branch=remote_branch,
        exc=exc,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=block.message,
        reason_code=block.reason_code,
    )


async def _protected_scope_push_block(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
    base_branch: str | None = None,
) -> _ProtectedScopePushBlock | None:
    if not worktree_path.exists():
        return None
    try:
        if base_branch is None:
            violations = await self._protected_scope_violations_for_unpushed_commits(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
            )
        else:
            violations = await self._protected_scope_violations_for_sync_base_push(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                base_branch=base_branch,
                remote_push_url=remote_push_url,
            )
    except ProtectedScopeDiffError as exc:
        return cast(
            _ProtectedScopePushBlock | None,
            await self._protected_scope_diff_unavailable_block(
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                exc=exc,
            ),
        )
    if not violations:
        return None
    message = quality_gate_violation_message(violations)
    await self._append_workspace_events(
        workspace_id=workspace_id,
        events=[
            WorkspaceEventCreate(
                event_type="workspace.monitor_protected_scope_push_blocked",
                reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
                payload={
                    "paths": _quality_gate_violation_paths(violations),
                    "protected_patterns": [violation.protected_pattern for violation in violations],
                    "violations": quality_gate_violation_details(violations),
                    "message": message,
                },
            )
        ],
    )
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        violations=tuple(violations),
    )


async def _enter_blocked_for_monitor_protected_violation(
    self: Any,
    *,
    workspace_id: str,
    violations: Sequence[QualityGateViolation],
) -> int | None:
    """Pause a ``monitoring_pr`` workspace into ``blocked`` (post-PR variant).

    The monitor-side counterpart to the executor's pre-PR
    ``enter_blocked_for_protected_violation``: it performs the
    ``monitoring_pr -> blocked`` CAS and stamps the SAME block-state columns via
    the shared :func:`apply_protected_block_columns` (block type/reason, epoch
    bump, cleared directive). The ``block_resume_phase`` is the monitor sentinel
    so the worker routes the resume back INTO the PR monitor. Returns the bumped
    ``block_epoch`` (always >= 1) when the CAS won (the row was still
    ``monitoring_pr``) so the caller reuses the same round-trip for the block
    notification key; returns ``None`` on a lost CAS, meaning the row already
    moved on and the caller must not claim a pause.
    """
    normalized = quality_gate_violation_details(violations)
    async with self._deps.session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            to=WorkspaceStatus.blocked,
            reason_code=PROTECTED_VIOLATION_BLOCKED_REASON_CODE,
            payload={
                "block_type": "protected_quality_gate",
                "resume_phase": MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
                "violations": normalized,
                "phase": "monitoring_pr",
            },
        )
        if ws is None:
            return None
        apply_protected_block_columns(
            ws,
            violations_normalized=normalized,
            resume_phase=MONITOR_PR_PROTECTED_PUSH_RESUME_PHASE,
        )
        await session.commit()
        return ws.block_epoch


async def _active_operator_grant_specs(self: Any, workspace_id: str) -> list[Any]:
    """Load the workspace's active-epoch operator grants for the monitor gate.

    Thin runner wrapper over the shared single source of truth so the post-PR
    protected-scope check honors an operator's approve-and-keep grant exactly
    like the pre-PR executor path."""
    return await load_active_operator_grant_specs(self._deps.session_factory, workspace_id)
