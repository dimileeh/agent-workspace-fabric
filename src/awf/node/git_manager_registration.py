"""Locked AWF linked-worktree registrations and dead-metadata handling (#1133).

Every agent container mounts the whole bare mirror read-write but only its own
checkout, at a different path. Inside a container every other registration's
``<mirror>/worktrees/<id>/gitdir`` target is missing, so one ``git worktree
prune`` there would delete every workspace's registration on the mirror. Git
never prunes a *locked* registration, so AWF registers its linked worktrees
locked, with a reason naming AWF and the workspace id.

The consequences handled here:

- reused checkouts created before the fix are locked on reuse;
- removal must override the lock (``worktree remove -f -f``);
- AWF's own reaping of a registration whose checkout is really gone can no
  longer rely on ``worktree prune`` alone. It unlocks only AWF-locked
  registrations of direct children of the managed worktrees root whose
  ``.git`` is gone (git's own prune criterion), under the mirror lock, then
  lets ``worktree prune`` remove them. Prune never deletes a working tree.
- hooks-path repair classifies dead or vanished registrations instead of
  failing the whole mirror (:func:`classify_linked_probe_failure`).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.common.logging import get_logger
from awf.node.git_manager_linked import _path_within_root
from awf.node.git_manager_ownership import git_env_without_object_lookup_overrides

if TYPE_CHECKING:
    from awf.node.git_manager import GitManager, GitOperationError

_log = get_logger(__name__)

AWF_WORKTREE_LOCK_REASON_PREFIX = "awf-managed worktree"
LOCK_FAILED_REASON_CODE = "GIT_WORKTREE_REGISTRATION_LOCK_FAILED"
REAPED_REASON_CODE = "GIT_WORKTREE_REGISTRATION_REAPED"
REAP_FAILED_REASON_CODE = "GIT_WORKTREE_REGISTRATION_REAP_FAILED"
DEAD_REASON_CODE = "MIRROR_WORKTREE_REGISTRATION_DEAD"
VANISHED_REASON_CODE = "MIRROR_WORKTREE_REGISTRATION_VANISHED"

# Only the first per-registration probes in ``_repair_mirror_hooks_path_once``
# are classified. Failures while unsetting or re-probing a value that *was*
# found stay fail-closed.
_LINKED_PROBE_OPERATIONS = frozenset(
    {"linked_worktree.hooks_path_probe", "worktree.hooks_path_probe"}
)

LinkedProbeFailure = Literal["vanished", "dead"]


def awf_worktree_lock_reason(workspace_id: str) -> str:
    """Return the ``git worktree lock`` reason AWF records for ``workspace_id``."""
    return f"{AWF_WORKTREE_LOCK_REASON_PREFIX} for workspace {workspace_id}; remove via AWF"


def worktree_add_lock_args(workspace_id: str) -> tuple[str, ...]:
    """Return ``worktree add`` args that register the worktree locked atomically."""
    # ``--reason`` is only accepted together with ``--lock``.
    return ("--lock", "--reason", awf_worktree_lock_reason(workspace_id))


def managed_worktrees_root_for_mirror(mirror_path: Path) -> Path | None:
    """Return the managed worktrees root for a ``work_dir/mirrors/<slug>.git`` mirror."""
    if mirror_path.parent.name != "mirrors":
        return None
    return mirror_path.parent.parent / "worktrees"


async def _run_mirror_worktree(mirror_path: Path, *args: str, operation: str) -> None:
    from awf.node.git_manager import GitOperationError

    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        str(mirror_path),
        "worktree",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_env_without_object_lookup_overrides(),
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    assert proc.returncode is not None
    if proc.returncode != 0:
        raise GitOperationError(
            operation=operation,
            returncode=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            reason_code=LOCK_FAILED_REASON_CODE,
        )


async def ensure_registration_locked(
    *, mirror_path: Path, worktree_path: Path, workspace_id: str
) -> bool:
    """Lock an existing registration that is not locked yet. Caller holds the mirror lock.

    Returns ``False`` (with a reason-coded log line) when the lock could not be
    added. The lock is protective: failing a healthy resume because it could not
    be added would turn the protection into a new outage path.
    """
    from awf.node.git_manager import GitOperationError

    if (mirror_path / "worktrees" / worktree_path.name / "locked").exists():
        return True
    try:
        await _run_mirror_worktree(
            mirror_path,
            "lock",
            "--reason",
            awf_worktree_lock_reason(workspace_id),
            str(worktree_path),
            operation="worktree.lock",
        )
    except GitOperationError as exc:
        if "already locked" in exc.stderr:
            return True
        _log.warning(
            "worktree.registration.lock_failed",
            reason_code=LOCK_FAILED_REASON_CODE,
            workspace_id=workspace_id,
            mirror_path=str(mirror_path),
            worktree_path=str(worktree_path),
            stderr=exc.stderr[:1000],
        )
        return False
    _log.info(
        "worktree.registration.locked",
        workspace_id=workspace_id,
        mirror_path=str(mirror_path),
        worktree_path=str(worktree_path),
    )
    return True


async def lock_reused_registration(
    *, mirror_path: Path, worktree_path: Path, workspace_id: str
) -> bool:
    """Lock a reused checkout's registration under the mirror lock."""
    from awf.node.git_manager import GitManager

    async with GitManager._lock_for_mirror(mirror_path):
        return await ensure_registration_locked(
            mirror_path=mirror_path, worktree_path=worktree_path, workspace_id=workspace_id
        )


def _dead_awf_locked_checkout(admin_dir: Path, worktrees_root: Path) -> Path | None:
    """Return the checkout of an AWF-locked registration that git would prune if unlocked."""
    try:
        lock_reason = (admin_dir / "locked").read_text(encoding="utf-8")
        raw_gitdir = (admin_dir / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        # Unlocked entries are plain prune's job. A missing back-reference cannot
        # prove the checkout lies in the managed root, and unreadable metadata
        # must stay on the fail-closed path.
        return None
    if not lock_reason.startswith(AWF_WORKTREE_LOCK_REASON_PREFIX) or not raw_gitdir:
        return None
    git_file = Path(raw_gitdir)
    if not git_file.is_absolute():
        git_file = admin_dir / git_file
    checkout = _path_within_root(git_file.parent, worktrees_root)
    if git_file.name != ".git" or checkout is None:
        return None
    if checkout.parent != worktrees_root.resolve() or os.path.lexists(git_file):
        return None
    return checkout


async def unlock_dead_managed_registrations(
    *, mirror_path: Path, worktrees_root: Path | None
) -> tuple[str, ...]:
    """Unlock AWF-locked registrations whose managed checkout is really gone.

    Caller holds the mirror lock and runs ``git worktree prune`` next, which
    re-checks "gone" itself and removes only admin dirs. Nothing is unlocked
    when the managed root is not visible (a container view of the mirror).
    """
    admin_root = mirror_path / "worktrees"
    if worktrees_root is None or not worktrees_root.is_dir() or not admin_root.is_dir():
        return ()
    try:
        admin_dirs = sorted(path for path in admin_root.iterdir() if path.is_dir())
    except OSError:
        return ()
    reaped: list[str] = []
    for admin_dir in admin_dirs:
        checkout = _dead_awf_locked_checkout(admin_dir, worktrees_root)
        if checkout is None:
            continue
        try:
            # Same effect as ``git worktree unlock``, without resolving a path
            # that no longer exists.
            (admin_dir / "locked").unlink(missing_ok=True)
        except OSError as exc:
            _log.warning(
                "worktree.registration.reap_failed",
                reason_code=REAP_FAILED_REASON_CODE,
                mirror_path=str(mirror_path),
                registration=admin_dir.name,
                error=str(exc),
            )
            continue
        _log.info(
            "worktree.registration.reaped",
            reason_code=REAPED_REASON_CODE,
            mirror_path=str(mirror_path),
            registration=admin_dir.name,
            worktree_path=str(checkout),
        )
        reaped.append(admin_dir.name)
    return tuple(reaped)


def classify_linked_probe_failure(
    exc: GitOperationError, *, linked_git_dir: Path, worktree_path: Path
) -> LinkedProbeFailure | None:
    """Classify a linked-registration hooks-path probe failure as stale metadata.

    - ``"vanished"``: the checkout, its ``.git`` or the admin dir disappeared
      between enumeration and probe.
    - ``"dead"``: the admin dir lacks ``HEAD`` or ``commondir``, so git cannot
      open the registration at all and it cannot carry hooks-path poisoning.
    - ``None``: anything else, including unreadable metadata. The caller
      re-raises and fails closed.
    """
    if exc.operation not in _LINKED_PROBE_OPERATIONS or exc.returncode != 128:
        return None
    stderr = exc.stderr.lower()
    if "not a git repository" not in stderr and "cannot change to" not in stderr:
        return None
    try:
        for path in (worktree_path, worktree_path / ".git", linked_git_dir):
            path.lstat()
    except FileNotFoundError:
        return "vanished"
    except OSError:
        return None
    try:
        for name in ("HEAD", "commondir"):
            (linked_git_dir / name).read_bytes()
    except FileNotFoundError:
        return "dead"
    except OSError:
        return None
    return None


def log_skipped_linked_registration(
    outcome: LinkedProbeFailure, *, mirror_path: Path, linked_git_dir: Path, worktree_path: Path
) -> None:
    """Record a dead or vanished registration skipped by hooks-path repair."""
    _log.warning(
        "mirror.hooks_path.dead_worktree_registration_skipped",
        reason_code=DEAD_REASON_CODE if outcome == "dead" else VANISHED_REASON_CODE,
        mirror_path=str(mirror_path),
        registration=linked_git_dir.name,
        worktree_path=str(worktree_path),
    )


async def remove_worktree_under_mirror_lock(
    manager: GitManager, *, workspace_id: str, mirror_path: Path, worktree_path: Path
) -> None:
    """Remove a (possibly locked) managed worktree and reap its registration."""
    from awf.node.git_manager import GitOperationError

    async with manager._lock_for_mirror(mirror_path):
        if worktree_path.exists():
            # A standalone repository (``.git`` as a directory) is not proven
            # AWF-linked leftover metadata. Fail closed rather than
            # ``rmtree`` — reclaiming would erase unrelated commits or
            # uncommitted work (AGENTS.md destructive-git rule; GC likewise
            # skips these paths as ``WORKTREE_NOT_GIT_MANAGED``).
            if (worktree_path / ".git").is_dir():
                raise GitOperationError(
                    operation="worktree.remove",
                    returncode=1,
                    stdout="",
                    stderr=(
                        f"refusing to remove standalone repository at "
                        f"{worktree_path}: .git is a directory (not a "
                        "linked-worktree gitfile); quarantine or remove it "
                        "manually before reclaiming the path"
                    ),
                    reason_code="GIT_WORKTREE_STANDALONE_REPO",
                )
            # ``--force`` because a failed task may leave dirty state; a second
            # ``--force`` because AWF registrations are locked.
            try:
                await manager._run(
                    [
                        "git",
                        "--git-dir",
                        str(mirror_path),
                        "worktree",
                        "remove",
                        "--force",
                        "--force",
                        str(worktree_path),
                    ],
                    operation="worktree.remove",
                )
            except GitOperationError as exc:
                # Idempotent removal: a directory left behind with stale git
                # metadata makes ``git worktree remove`` fail with
                # ``fatal: '<path>' is not a working tree``. If the worktree
                # ``.git`` file was already removed but mirror metadata still
                # points to it, Git instead reports that validation failed
                # because ``<path>/.git`` does not exist. If the gitfile is
                # present but linked admin metadata is corrupt (e.g. missing
                # HEAD), Git reports ``.git`` is not a .git file with
                # error code 7. Do not treat other codes (e.g. 3 for an
                # unreadable gitfile) as reclaimable — that would erase a
                # live checkout. Standalone ``.git`` dirs are refused
                # above. Re-raise any genuine removal error.
                stderr = exc.stderr.lower()
                corrupt_or_missing_git_file = (
                    "validation failed, cannot remove working tree" in stderr
                    and ".git" in stderr
                    and (
                        "does not exist" in stderr
                        or ("is not a .git file" in stderr and "error code 7" in stderr)
                    )
                )
                if "is not a working tree" not in stderr and not corrupt_or_missing_git_file:
                    raise
                # ``git worktree remove`` never ran, so the physical
                # directory and its contents are still on disk; ``worktree
                # prune`` below only clears metadata for *missing* dirs and
                # would leave the disk space behind. Reclaim it ourselves so
                # GC actually frees the space it reports as reclaimed. We must
                # NOT swallow genuine deletion failures (e.g. permission
                # errors): if rmtree fails the directory is still on disk, and
                # callers like ``WorkspaceCleanupService.cleanup_workspace``
                # rely on a raised error to record the step as partial/failed
                # instead of falsely reporting removal success.
                await asyncio.to_thread(manager._reclaim_stale_worktree, worktree_path)
                _log.info(
                    "worktree.remove.already_gone",
                    workspace_id=workspace_id,
                    worktree_path=str(worktree_path),
                )

        if mirror_path.exists():
            # Prune skips locked entries: unlock AWF registrations whose
            # managed checkout is gone first (stale reclaim, out-of-band delete).
            await unlock_dead_managed_registrations(
                mirror_path=mirror_path, worktrees_root=manager._worktrees_dir
            )
            await manager._run(
                ["git", "--git-dir", str(mirror_path), "worktree", "prune"],
                operation="worktree.prune",
            )
