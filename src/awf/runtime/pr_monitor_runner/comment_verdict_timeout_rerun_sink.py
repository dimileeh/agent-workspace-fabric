"""Dirty-edit salvage for a timed-out run the service-recovery loop reruns over.

Kept in a sibling module so ``comment_verdict`` stays under the line budget. The
sink, residue probe, and outcome type resolve through ``comment_verdict`` at call
time so a monkeypatch there still reaches them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
    _fingerprint_has_pr_worthy_path_residue,
)
from awf.runtime.pr_monitor_runner.constants import _TaskTagUnset

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


_log = get_logger("awf.runtime.pr_monitor_runner.comment_verdict")


async def sink_timeout_rerun_dirty_changes(
    runner: PullRequestMonitorRunner,
    *,
    reason_code: str,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None,
    task_tag: str | None | _TaskTagUnset,
    command_evidence: list[str],
    commit_dirty_changes: bool,
) -> bool:
    """Commit a reran-over timed-out run's uncommitted edits (#932).

    A published rerun floor is only a SHA, so it cannot hold edits the
    timed-out run never committed: with no commits of its own that floor
    equals the attempt's own floor, and the rollback a provider or protocol
    failure on the rerun performs resets straight through them. Running the
    preserve handler's own dirty sink first turns them into a commit the
    published floor does cover (PRRT_kwDOSJAM6s6fvw8r). The caller passes
    ``item_start_head`` live: it is the sink anchor as of the run this
    salvage belongs to.

    Returns whether the recovery loop may rerun over this run. The sink's own
    False is not that answer: it also means "nothing to commit" (clean
    worktree, or the sink disabled), which is the ordinary case and must not
    cost the rerun. Only stranded PR-worthy dirt does — the salvage failed,
    the published SHA floor cannot cover those edits, and the rollback a
    provider failure or non-FIXED verdict on the rerun performs would delete
    work #932 promised to keep. Probe for it and fail closed on an unreadable
    probe: preserving the timeout costs one rerun, guessing costs the edits
    (PRRT_kwDOSJAM6s6fwTyO).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    sink_outcome = await _comment_verdict._sink_timeout_dirty_changes(
        runner,
        workspace_id=workspace_id,
        reason_code=reason_code,
        item_start_head=item_start_head,
        commit_message=commit_message,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=task_tag,
        command_evidence=command_evidence,
        commit_dirty_changes=commit_dirty_changes,
    )
    if sink_outcome is _comment_verdict.TimeoutSinkOutcome.COMMITTED:
        return True
    try:
        residue_fp = await _comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
    except Exception as probe_exc:
        # Broad on purpose, like every other residue probe here: it spawns
        # Git and can raise outside the git-spawn error set. Fail closed —
        # an unreadable worktree cannot prove the edits were salvaged.
        # ``asyncio.CancelledError`` is a ``BaseException`` and still
        # propagates.
        _log.warning(
            "monitor.agent_verdict_timeout_rerun_residue_probe_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
            exc_type=type(probe_exc).__name__,
        )
        return False
    if residue_fp is None:
        return False
    return not _fingerprint_has_pr_worthy_path_residue(residue_fp)
