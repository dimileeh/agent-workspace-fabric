"""Runner step: confirm a ``fixed`` disposition that crossed a moved head live (#1138, D2).

On re-adoption after the PR head moved, a bound ``fixed`` /
``fixed_branch_evidence`` disposition crosses live with
``__awf_disposition_readopt_pending__:<thread id>`` = the adopted head
(:func:`~awf.service.pr_monitor_adoption_seed.seedable_monitor_state`), the way
AWF already treats ``fixed`` within one workspace across later commits. This
step runs before ``decide()`` on the successor's poll and checks every commit the
disposition cites with ``git merge-base --is-ancestor <commit> <adopted head>``.

It fails closed: all ancestors carries the record (``..._READOPT_CARRIED``);
anything else — exit 1, any other exit, a timeout or OS error, or a record that
is missing or no longer a posted ``fixed`` reply with commits — retires it with
the tombstone the seed used to write (``..._READOPT_RETIRED``), and the #1138
fallback re-triages the thread. The thread must also still be resolved by AWF's
identity with its conversation unchanged since that reply; the gate's binding
check enforces both, so they need no code here. Until this step ran, the gate
certifies nothing on the record.
"""

from __future__ import annotations

import re
from typing import Any

from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import (
    _disposition_readopt_pending_key,
    _disposition_retired_key,
)
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.thread_disposition import (
    REVIEW_THREAD_DISPOSITION_READOPT_CARRIED,
    REVIEW_THREAD_DISPOSITION_READOPT_RETIRED,
    ThreadDisposition,
    read_item_disposition,
    retire_item_disposition,
)

_AUDIT_ACTION = "review_thread_disposition_readopt_check"
_PROBE_TIMEOUT_SECONDS = 60.0
_LIVE_CLASSES = frozenset({"fixed", "fixed_branch_evidence"})
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


async def verify_readopted_dispositions(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """Carry or retire every disposition awaiting its readopt check; no-op without any."""
    prefix = _disposition_readopt_pending_key("")
    pending = sorted(k[len(prefix) :] for k in state.threads_addressed_ids if k.startswith(prefix))
    if not pending:
        return
    carried: dict[str, str] = {}
    retired: dict[str, str] = {}
    for thread_id in pending:
        adopted_head = state.threads_addressed_ids.pop(prefix + thread_id)
        disposition = read_item_disposition(state, thread_id)
        problem = await _ancestry_problem(self, workspace_id, disposition, adopted_head)
        if problem is None:
            carried[thread_id] = "ancestor"
            continue
        if disposition is not None:
            retire_item_disposition(state, disposition)
        else:
            # Nothing to retire: no marker in the thread certifies without a record.
            state.mark_addressed(_disposition_retired_key(thread_id, ""), "retired")
        retired[thread_id] = problem
    await self._persist_state(workspace_id, state)
    for outcome, reason_code, details in (
        ("carried", REVIEW_THREAD_DISPOSITION_READOPT_CARRIED, carried),
        ("retired", REVIEW_THREAD_DISPOSITION_READOPT_RETIRED, retired),
    ):
        if not details:
            continue
        log = _log.info if outcome == "carried" else _log.warning
        log(
            "monitor.review_thread_disposition_readopt_check",
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_ids=list(details),
            details=details,
            reason_code=reason_code,
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action=_AUDIT_ACTION,
            outcome=outcome,
            reason_code=reason_code,
            pr_number=pr_number,
            status=None,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            evidence={"thread_ids": list(details), "details": details},
        )


async def _ancestry_problem(
    self: Any, workspace_id: str, disposition: ThreadDisposition | None, adopted_head: str
) -> str | None:
    """``None`` when every cited commit is an ancestor of ``adopted_head``, else why not."""
    if disposition is None:
        return "record_missing"
    if (
        disposition.disposition_class not in _LIVE_CLASSES
        or not disposition.reply_comment_id
        or disposition.reread_pending
        or not disposition.commits
    ):
        return "record_not_live"
    head = adopted_head.strip().lower()
    commits = [commit.strip().lower() for commit in disposition.commits]
    if _FULL_SHA_RE.match(head) is None or any(_COMMIT_RE.match(c) is None for c in commits):
        return "probe_failed"  # never hand git an argument that is not a commit id
    worktree = self._worktrees_root / workspace_id
    for commit in commits:
        try:
            result = await self._deps.runner.run(
                git_worktree_command(worktree, "merge-base", "--is-ancestor", commit, head),
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )
        except OSError:
            return "probe_failed"
        if result.returncode == 1:
            return "not_ancestor"
        if result.returncode != 0:
            return "probe_failed"
    return None
