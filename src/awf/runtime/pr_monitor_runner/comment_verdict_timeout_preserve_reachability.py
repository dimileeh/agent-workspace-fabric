"""Is a re-armed item-start anchor still reachable from this attempt? (#934 audit)

Re-arming the preserved-timeout marker on every attempt that dies before a verdict
lets it outlive several failed passes — long enough for a ``SyncBase`` rebase to
rewrite the branch and strand the anchor on a dropped SHA. This module owns the
bounded Git probes that answer "is that anchor still an ancestor of where this
attempt starts?", plus the worktree presence check that runs in front of them.

Every probe here fails *open*: only a definitive "not an ancestor" or a provably
missing object drops the anchor, because dropping it also costs the preserved
commits their place in the item's own evidence range.

Kept in a sibling module so ``comment_verdict_timeout_preserve`` stays under the
first-party line budget; re-exported from there (``X as X``) so monkeypatch seams
and existing imports keep working.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from awf.adapters.worktree_activity import (
    WorktreeProbeCapacityError,
    probe_worktree_filesystem,
)
from awf.common.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

# ``git merge-base --is-ancestor`` answers "no" with exit 1; every other non-zero
# exit is an error, not an answer. ``git rev-parse --verify --quiet`` likewise
# uses exit 1 for "this name resolves to nothing".
_GIT_NOT_AN_ANCESTOR_RETURN_CODE = 1
_GIT_UNRESOLVABLE_NAME_RETURN_CODE = 1

# Bounds the worktree presence check in front of the anchor probes. Generous
# next to an ordinary ``stat``, small next to the monitor loop it must never
# wedge — the bound the recovery loop's own presence probe already uses.
_WORKTREE_PRESENCE_PROBE_TIMEOUT_SECONDS = 10.0


async def preserved_anchor_is_reachable(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    anchor_head: str,
    attempt_start_head: str | None,
) -> bool:
    """Is a preserved anchor still an ancestor of this attempt's start HEAD?

    Re-arming the marker on every attempt that dies before a verdict (#934 audit)
    lets it outlive several failed passes — long enough for a ``SyncBase`` rebase
    to rewrite the branch and strand the anchor on a dropped SHA. Anchoring a
    later attempt there gives an evidence range git cannot resolve, so an honest
    ``FIXED`` can never be proven and the item wedges. Only a definitive "not an
    ancestor" answer drops the anchor: an unreadable probe keeps it, because
    dropping it also costs the preserved commits their place in the item's own
    evidence range.

    ``merge-base --is-ancestor`` spells that definitive answer as exit 1 alone.
    Every other non-zero exit is a non-answer — the command runner reports its own
    timeout as exit 124, and git fatals (a broken worktree, a locked repo, an
    unreadable object store) exit 128 — so those fall through to a direct
    existence probe on the anchor rather than being read as "not an ancestor"
    (#934 audit). A pruned anchor object is the stranding this guard exists for
    and still drops; anything else keeps it.

    The presence check in front of them is bounded for the same reason they are
    (PRRT_kwDOSJAM6s6f5q9B).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    if attempt_start_head is None or anchor_head.lower() == attempt_start_head.lower():
        return True
    if not await _worktree_is_definitely_present(worktree_path, anchor_head=anchor_head):
        return True
    try:
        result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                anchor_head,
                attempt_start_head,
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_probe_failed",
            anchor_head=anchor_head,
            attempt_start_head=attempt_start_head,
            exc_type=type(probe_exc).__name__,
        )
        return True
    if result.ok:
        return True
    if result.returncode == _GIT_NOT_AN_ANCESTOR_RETURN_CODE:
        return False
    _log.warning(
        "monitor.agent_verdict_item_start_head_probe_inconclusive",
        anchor_head=anchor_head,
        attempt_start_head=attempt_start_head,
        returncode=result.returncode,
        reason_code=result.reason_code,
    )
    return not await _anchor_object_is_missing(
        runner,
        worktree_path=worktree_path,
        anchor_head=anchor_head,
    )


async def _worktree_is_definitely_present(worktree_path: Path, *, anchor_head: str) -> bool:
    """Is the worktree definitively there — bounded, and never raising.

    ``Path.exists()`` is a synchronous ``stat``, and this one runs on the monitor
    worker's event-loop thread, ahead of both bounded Git probes, over the
    worktree a timed-out agent was last touching. Against a wedged FUSE/NFS mount
    that ``stat`` blocks uninterruptibly and freezes the whole worker — unrelated
    workspaces included — and it only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so a
    transient EIO/EACCES escapes this guard as an unrelated exception instead of a
    verdict (PRRT_kwDOSJAM6s6f5q9B).

    The bound only ends the *wait*, though: a ``stat`` parked in the kernel cannot
    be cancelled, so the thread underneath it runs on until the filesystem
    answers. On the process-wide default executor ``asyncio.to_thread`` submits
    to, enough wedged workspaces would leave every worker the rest of the control
    plane's ``to_thread`` work — Git, Docker, GC — draws from occupied long after
    each guard returned, and ``concurrent.futures``' interpreter-exit join would
    hold a graceful worker restart up behind them. So this borrows the worktree
    scanner's abandonable daemon-thread mechanism, whose process-wide ceiling
    keeps the worst case at a fixed number of threads nothing can reclaim — the
    same mechanism the recovery loop's own presence probe already uses
    (PRRT_kwDOSJAM6s6f6Co6).

    Anything short of a definitive answer is therefore unknown. Unknown reads as
    "not definitely present", which keeps the anchor: the same fail-open the
    unreadable-probe paths above take, and the answer the Git probes themselves
    reach on a worktree they cannot read.
    """
    try:
        return await asyncio.wait_for(
            probe_worktree_filesystem(worktree_path.exists, worktree_path=str(worktree_path)),
            timeout=_WORKTREE_PRESENCE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, WorktreeProbeCapacityError) as probe_exc:
        # ``TimeoutError`` is an ``OSError`` subclass, so the stalled-mount and
        # unreadable-path cases share this handler; a worker already holding every
        # probe thread it allows is the same "could not tell", and starts no
        # thread of its own — which is what the ceiling is for.
        # ``asyncio.CancelledError`` is a ``BaseException`` and still propagates.
        _log.warning(
            "monitor.agent_verdict_item_start_head_presence_probe_failed",
            anchor_head=anchor_head,
            exc_type=type(probe_exc).__name__,
        )
        return False


async def _anchor_object_is_missing(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    anchor_head: str,
) -> bool:
    """Is ``anchor_head`` provably absent from this worktree's object store?

    Reached only when the ancestry probe could not answer. ``git rev-parse
    --verify --quiet <sha>^{commit}`` exits 1 exactly when the name resolves to
    nothing — the pruned-anchor stranding — while a broken repo or a probe
    timeout exits 128 / 124 and proves nothing, so anything but that definitive 1
    keeps the anchor.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    try:
        result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{anchor_head}^{{commit}}",
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.agent_verdict_item_start_head_existence_probe_failed",
            anchor_head=anchor_head,
            exc_type=type(probe_exc).__name__,
        )
        return False
    return result.returncode == _GIT_UNRESOLVABLE_NAME_RETURN_CODE
