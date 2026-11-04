"""Sync-base support helpers split out of :mod:`remote_ops` (line budget).

``_refresh_staleness_after_sync_base`` moved here unchanged; ``remote_ops``
re-exports it for existing call sites. ``_resolve_merged_base_sha`` records the
base commit a sync-base merge actually integrated (#1167).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.audit import redact_audit_text
from awf.common.logging import get_logger
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.constants import _SYNC_BASE_RESOLVABLE_STALE_REASONS

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _merged_base_sha_from_rev_parse(
    *,
    workspace_id: str,
    ref: str,
    returncode: int,
    stdout: str,
    stderr: str,
) -> str | None:
    """Normalize a merged-base ``git rev-parse`` result, or ``None`` (logged).

    ``None`` lets the ``sync_base_push`` audit fall back to
    ``workspace.base_commit`` exactly as before #1167.
    """
    sha = (stdout or "").strip().lower()
    if returncode == 0 and _SHA_RE.fullmatch(sha):
        return sha
    _log.warning(
        "monitor.sync_base_merged_base_unresolved",
        workspace_id=workspace_id,
        ref=ref,
        returncode=returncode,
        stderr=redact_audit_text(stderr or "", limit=400),
    )
    return None


async def _resolve_merged_base_sha(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    ref: str,
) -> str | None:
    """Resolve ``ref`` (the clean merge commit's ``HEAD^2``) for the audit."""
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

    result = await runner._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", ref),
        env=git_env_without_object_lookup_overrides(),
    )
    return _merged_base_sha_from_rev_parse(
        workspace_id=workspace_id,
        ref=ref,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


async def _refresh_staleness_after_sync_base(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    base_branch: str,
) -> None:
    """Refresh candidate stale state after a successful sync-base repair."""
    from awf.db.repositories import (
        MergeCandidateRepository,
        StaleReasonCreate,
        StaleReasonRepository,
        sync_candidate_readiness,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

    try:
        async with runner._deps.session_factory() as session:
            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is None:
                # No tracked candidate — nothing to refresh.
                return
            active_reasons = await StaleReasonRepository(session).list_active_for_candidate(
                candidate.id
            )
            resolvable = [
                r for r in active_reasons if r.reason_code in _SYNC_BASE_RESOLVABLE_STALE_REASONS
            ]
            worktree_path = runner._worktrees_root / workspace_id
            rev_parse = await runner._deps.runner.run(
                git_worktree_command(worktree_path, "rev-parse", f"origin/{base_branch}")
            )
            if rev_parse.returncode != 0 or not rev_parse.stdout.strip():
                _log.warning(
                    "monitor.sync_base_staleness_refresh_skipped",
                    workspace_id=workspace_id,
                    reason="rev_parse_failed",
                    stderr=(rev_parse.stderr[:400] if rev_parse.stderr else None),
                )
                return
            new_base_sha = rev_parse.stdout.strip()
            candidate.base_sha = new_base_sha
            if not resolvable:
                # No target-derived rows to clear. Leave any intrinsic
                # blocking reason (e.g. ``docs_task_scope_violation``) and
                # the candidate's stale flag untouched — a rebase does not
                # remediate those — and just persist the advanced base.
                await session.commit()
                return
            # Resolve only the target-derived rows by re-stating every other
            # active finding: ``replace_active_findings`` keeps rows whose
            # (reason_code, trigger_type, trigger_ref) key is supplied and
            # resolves the rest, so the preserved intrinsic rows survive.
            preserved = [
                r
                for r in active_reasons
                if r.reason_code not in _SYNC_BASE_RESOLVABLE_STALE_REASONS
            ]
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=candidate.workspace_id,
                candidate_id=candidate.id,
                attempt_id=candidate.attempt_id,
                task_id=candidate.task_id,
                findings=[
                    StaleReasonCreate(
                        reason_code=r.reason_code,
                        trigger_type=r.trigger_type,
                        trigger_ref=r.trigger_ref,
                        explanation=r.explanation,
                    )
                    for r in preserved
                ],
            )
            # The candidate's stale_reason column may still hold a reason we
            # just resolved (or a generic "stale"). Clear it, then let
            # sync_candidate_readiness reinstate only the intrinsic reasons
            # (docs scope / validation tier) that genuinely still apply.
            candidate.stale = False
            candidate.stale_reason = None
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
                sync_validation_staleness=True,
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort; reconciler will retry
        _log.warning(
            "monitor.sync_base_staleness_refresh_failed",
            workspace_id=workspace_id,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
