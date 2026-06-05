"""Shared read-only ``~/.claude`` overlay base build/reap for workspace auth.

Split out of :mod:`awf.node.auth_mounts_claude` to keep that module under the
first-party line limit. This module holds the host-content signature hashing and
the host-wide shared read-only base lifecycle (build-once-per-signature, atomic
staging, crash-orphan reap) that the per-workspace overlay mount in
:mod:`awf.node.auth_mounts_claude` consumes. The dependency is one-directional:
nothing here imports from :mod:`awf.node.auth_mounts_claude`, which re-imports
these names so ``awf.node.auth_mounts.<name>`` stays the stable import surface
for callers and tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from awf.common.logging import get_logger
from awf.node.auth_mounts_claude_reconcile import (
    _CLAUDE_USAGE_HISTORY_DIRS as _CLAUDE_USAGE_HISTORY_DIRS,
)

_log = get_logger(__name__)

# Shared, read-only overlay base for ``~/.claude``. The bulk of ``~/.claude``
# (skills, plugins, static config) is identical across workspaces and read-only
# at runtime, so a single host-wide base snapshot is reused as the overlay
# ``lowerdir`` instead of copying ~1.7 GB per workspace. The base lives outside
# any ``auth/<workspace_id>`` dir so GC (which enumerates candidates from DB
# workspace rows) can never reap it.
_SHARED_AUTH_DIRNAME = "_shared"
_CLAUDE_BASE_DIRNAME = "claude-base"
# A hard kill (OOM, SIGKILL) between ``mkdtemp`` and the ``finally`` cleanup in
# ``_ensure_shared_claude_base`` strands a ``.claude-base-*`` staging dir (a full
# ~1.7 GB copy of ``~/.claude``) under ``_shared``, which GC never reaps. Each
# active build holds an exclusive ``flock`` on this marker for the whole copy; the
# next provision's reaper decides reapability by lock *liveness* (the kernel frees
# the lock on process death) rather than an elapsed-time heuristic, so a build that
# legitimately runs for hours — a large or network-mounted ``~/.claude`` — is never
# reaped out from under itself, while a crash-orphaned staging dir (lock free or
# marker absent) always is.
_CLAUDE_BASE_BUILD_LOCK_NAME = ".build.lock"
_CLAUDE_AUTH_SHARED_BASE_FAILED = "CLAUDE_AUTH_SHARED_BASE_FAILED"


def _shared_claude_base_dir(work_dir: Path, signature: str) -> Path:
    """Return the host-wide shared read-only ``~/.claude`` base for ``signature``.

    The host-content ``signature`` names the base dir so a changed host
    ``~/.claude`` (operator added/updated skills, plugins, settings, tokens, …)
    builds a *fresh* base instead of reusing a stale one. Bases are immutable
    once built: a new signature gets a new dir and old signature dirs are left
    untouched, so a workspace that still has an old base mounted as its overlay
    lowerdir keeps a consistent view (overlayfs forbids mutating a live lower).
    """

    return work_dir / "auth" / _SHARED_AUTH_DIRNAME / _CLAUDE_BASE_DIRNAME / signature / ".claude"


def _host_claude_signature(host_home: Path) -> str:
    """Return a content signature of the host ``~/.claude`` overlay source.

    Lets a new workspace notice that an operator changed ``~/.claude`` since the
    shared base was last built, so it rebuilds under a fresh signature rather
    than mounting a stale lowerdir (the legacy per-workspace copy always
    reflected the current host; the shared base must not silently fall behind).
    The usage-history dirs excluded from the copied base are excluded here too,
    so churn in those transcript trees never forces a needless rebuild. Cheap:
    ``stat`` only, no file reads.

    Symlinks are followed (``followlinks=True`` + ``stat`` rather than ``lstat``)
    to mirror the copy, which uses ``copytree(symlinks=False)`` and so copies the
    *targets'* contents into the base. If an operator keeps skills/plugins/settings
    as symlinks into a dotfiles repo and updates a target without replacing the
    link, the link's own ``lstat`` is unchanged — signing on ``lstat`` would reuse a
    stale base while the copy would have refreshed it. Signing the resolved targets
    keeps the signature aligned with what actually gets copied.

    ``st_mode`` is part of the key too. ``copytree`` uses ``copy2``, which preserves
    permission bits, so an operator running ``chmod +x`` on a plugin/hook script
    inside ``~/.claude`` changes what the copy produces. ``chmod`` bumps ``ctime``
    but leaves ``st_size`` and ``st_mtime_ns`` untouched, so signing on size+mtime
    alone would reuse a stale base that lacks the new mode bits. Including the mode
    rebuilds when permissions change.

    ``st_ctime_ns`` is part of the key too (the #382 design call). size+mtime+mode
    alone miss a *metadata-preserving* content rewrite — a dotfile manager, ``cp -p``,
    or a backup restore that replaces a Claude config / skill / token while restoring
    the original ``st_mtime_ns`` and length. The kernel bumps ``st_ctime`` on *any*
    inode change, and the final ``utime`` that restores the mtime itself re-bumps
    ``ctime``, so a ctime that moved while mtime/size held flags exactly the edit the
    other fields hide, forcing a fresh base. It is a free field from the same
    ``stat()`` call — no extra I/O, no content read — so the deliberate stat-only /
    no-1.7 GB-read contract is intact (content-hashing was rejected for that reason).
    The trade-off: ``ctime`` is not settable from userspace, so a content-identical
    *revert* no longer reproduces an old signature; recovery of a surviving overlay
    ``upper`` after host churn instead flows through the ``base.signature`` pin
    (:func:`_pinned_overlay_base`), which records the exact base and is immune to
    reverts and metadata edits.

    ``followlinks=True`` does not detect symlink cycles, so a circular link in
    ``~/.claude`` (e.g. a skills dir linked back to a parent) would otherwise loop
    forever — and unlike the legacy per-workspace ``copytree`` this runs on every
    provision call, so one cycle would silently hang all future provisioning. Each
    walked directory path carries the set of resolved ``(st_dev, st_ino)`` identities
    of its *own ancestors*; a child whose identity is already an ancestor is a true
    cycle and is pruned before descent. Crucially this prunes only ancestor cycles,
    not every repeat of an inode: two directory symlinks to the same target are not
    a cycle, and ``copytree(symlinks=False)`` copies each linked path separately, so
    both must be walked and signed — deduping them globally would leave the signature
    blind to a second link and reuse a base missing content reachable through it.
    """

    source = host_home / ".claude"
    excluded = frozenset(_CLAUDE_USAGE_HISTORY_DIRS)
    entries: list[str] = []
    # Per-branch ancestor identities, keyed by walked path. Seeded with the source's
    # own identity so a child linking straight back to ``~/.claude`` is caught.
    ancestors_by_path: dict[str, frozenset[tuple[int, int]]] = {}
    try:
        source_stat = source.stat()
        ancestors_by_path[os.fspath(source)] = frozenset({(source_stat.st_dev, source_stat.st_ino)})
        # ``copytree`` preserves the top-level directory's mode, so the root's own
        # ``st_mode`` is part of what the copy produces. The walk below only signs
        # the root's *children*, so without this an operator fixing ``~/.claude``
        # itself (e.g. an inaccessible mode to ``0700``) would leave the signature
        # unchanged and keep reusing a base with stale root permissions.
        entries.append(
            f".\0{source_stat.st_size}\0{source_stat.st_mtime_ns}\0"
            f"{source_stat.st_ctime_ns}\0{source_stat.st_mode}"
        )
    except OSError:
        ancestors_by_path[os.fspath(source)] = frozenset()
        entries.append(".\0missing")
    for root, dirs, files in os.walk(source, followlinks=True):
        root_path = Path(root)
        root_ancestors = ancestors_by_path.get(os.fspath(root_path), frozenset())
        kept: list[str] = []
        for name in dirs:
            if name in excluded:
                continue
            child = root_path / name
            try:
                dir_stat = child.stat()
            except OSError:
                # A dangling/broken dir link: keep it so the entry loop below
                # records the ``missing`` marker, matching the prior behaviour.
                # Record an ancestors entry too, so the invariant "every walked
                # path has an entry" holds: if the link target is created in the
                # window before ``os.walk`` descends, the descendant branch still
                # inherits this root's ancestor set instead of an empty one,
                # keeping cycle detection sound.
                ancestors_by_path[os.fspath(child)] = root_ancestors
                kept.append(name)
                continue
            identity = (dir_stat.st_dev, dir_stat.st_ino)
            if identity in root_ancestors:
                # True ancestor cycle: descending re-enters a directory we are
                # already inside. Prune to bound the walk. A distinct link to the
                # same target that is *not* an ancestor is kept and walked.
                continue
            ancestors_by_path[os.fspath(child)] = root_ancestors | {identity}
            kept.append(name)
        dirs[:] = kept
        for name in (*dirs, *files):
            entry = root_path / name
            rel = entry.relative_to(source).as_posix()
            try:
                entry_stat = entry.stat()
            except OSError:
                entries.append(f"{rel}\0missing")
                continue
            entries.append(
                f"{rel}\0{entry_stat.st_size}\0{entry_stat.st_mtime_ns}\0{entry_stat.st_ctime_ns}\0{entry_stat.st_mode}"
            )
    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()
    return digest[:16]


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        os.lchown(path, uid, gid)
        return

    os.chown(path, uid, gid)
    if not path.is_dir():
        return

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)


def _ensure_shared_claude_base(
    *,
    host_home: Path,
    work_dir: Path,
    signature: str,
    workspace_owner_uid: int | None,
    workspace_owner_gid: int | None,
) -> Path:
    """Build (once per host-content signature) the shared ``~/.claude`` base.

    The base dir is keyed by a signature of the current host ``~/.claude`` so a
    later workspace whose host changed gets a freshly built base instead of a
    stale lowerdir; an unchanged host reuses the existing one. Built into a temp
    dir and moved into place atomically so a concurrent provision either sees a
    complete base or loses the rename race and reuses the winner's. Chowned once
    to the agent uid/gid (all workspace agents share uid 1000) so the read-only
    lower is readable through the overlay.

    Superseded-base accumulation: each distinct host ``~/.claude`` signature
    builds a new ``<sig>/.claude`` base (~1.7 GB) and leaves prior-signature bases
    in place (``_reap_stale_claude_base_staging`` only reclaims crash-orphaned
    ``.claude-base-*`` *staging* dirs, and terminal-workspace GC only enumerates
    ``auth/<workspace_id>`` rows, never ``_shared``). Those completed-but-superseded
    base dirs are reclaimed by GC-B (:func:`awf.service.gc_claude_base.
    reap_superseded_claude_bases`), which skips the current signature and any base
    that is live-mounted (``lowerdir=`` in ``/proc/mounts``) or pinned by a
    surviving workspace before removing it — overlayfs forbids mutating/removing a
    live lower.
    """

    base = _shared_claude_base_dir(work_dir, signature)
    # Sweep crash-orphaned staging on *every* provision — before the existing-base
    # early return below, not only on the build path. Once the base for a
    # signature exists every later call returns early, so an orphan that outlived
    # its build window (too young to reap when the base was built, then later
    # stale) — or one stranded under a now-superseded signature on a host whose
    # ``~/.claude`` stopped changing — would never be revisited, and GC never
    # enters ``_shared``, permanently stranding the ~1.7 GB copy. Sweep from the
    # signature-root (``base.parent.parent``, parent of every ``<signature>``
    # dir): a crash strands an orphan under whatever signature was current then,
    # so a per-signature sweep would never revisit an old signature's orphan.
    _reap_stale_claude_base_staging(base.parent.parent)
    if base.is_dir():
        return base

    shared_root = base.parent
    shared_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".claude-base-", dir=shared_root))
    staged_base = staging / ".claude"
    build_lock = staging / _CLAUDE_BASE_BUILD_LOCK_NAME
    # Hold an exclusive advisory ``flock`` on the staging dir for the whole build.
    # The reaper keys reapability off this lock's *liveness*, not elapsed time, so a
    # slow-but-live build (a large or network-mounted ``~/.claude`` running over an
    # hour) is never reaped out from under itself (#379). Acquiring the lock is the
    # first action after ``mkdtemp``, so the unlocked window is a sub-millisecond
    # ``open``; a concurrent reaper that races into it only forces this provision to
    # rebuild (the staging dir is discarded — no data loss), never corrupts a live
    # build. The kernel releases the lock automatically on process death
    # (OOM/SIGKILL/crash) — exactly the orphan case a duration heuristic could not
    # tell apart from a legitimately long build.
    lock_fd = None
    # ``finally`` guarantees the lock fd is closed (releasing the lock) and the
    # staging dir is reclaimed on every exit: an ``os.open`` OSError (disk full,
    # permissions) before the lock is even acquired, a copytree/chown OSError that
    # propagates to the caller's legacy-copy fallback, a lost build race, or the
    # success path (where ``replace`` has already moved ``staged_base`` out).
    # ``os.open`` lives inside the ``try`` (with ``lock_fd`` pre-set to ``None``) so
    # that a failure creating the lock file still reclaims the ``mkdtemp`` staging
    # tree rather than orphaning it under the shared root.
    try:
        lock_fd = os.open(build_lock, os.O_CREAT | os.O_WRONLY, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutil.copytree(
            host_home / ".claude",
            staged_base,
            ignore=shutil.ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS),
            ignore_dangling_symlinks=True,
        )
        if workspace_owner_uid is not None and workspace_owner_gid is not None:
            _chown_tree(staged_base, workspace_owner_uid, workspace_owner_gid)
        try:
            staged_base.replace(base)
        except OSError as exc:
            if base.is_dir():
                # Lost the build race against a concurrent provision; reuse its
                # base. The winner's ``replace`` populated ``base``, so renaming
                # onto the now non-empty directory fails — that is the race, and
                # ``base.is_dir()`` distinguishes it from a genuine failure.
                return base
            # ``base`` does not exist, so this is not a lost race but a real
            # failure (e.g. permissions). Surface it with log evidence and
            # re-raise so the caller degrades to the legacy copy visibly rather
            # than silently returning a non-existent base on every provision.
            _log.warning(
                "claude_auth_shared_base_replace_failed",
                reason_code=_CLAUDE_AUTH_SHARED_BASE_FAILED,
                staging=str(staging),
                base=str(base),
                error=str(exc),
            )
            raise
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        shutil.rmtree(staging, ignore_errors=True)
    return base


def _reap_stale_claude_base_staging(base_root: Path) -> None:
    """Remove crash-orphaned ``.claude-base-*`` staging dirs under ``base_root``.

    ``_ensure_shared_claude_base`` builds the base in a ``mkdtemp`` staging dir
    (under the current ``<signature>`` dir) and its ``finally`` reclaims it on
    every normal exit, but a hard kill (OOM, SIGKILL, crash) before that cleanup
    strands the staging tree — a full ~1.7 GB copy of ``~/.claude`` — under
    ``_shared``, which GC deliberately never reaps. Sweeping here on the next
    provision keeps those orphans from accumulating indefinitely.

    ``base_root`` is the parent of every ``<signature>`` dir, so the sweep covers
    *all* signatures (``*/.claude-base-*``), not just the one current now. A crash
    orphans staging under whatever signature was current at the time; if the host
    ``~/.claude`` changes before the next provision the current signature moves,
    and a sweep scoped to only the new signature's dir would never revisit — and
    so never reap — the old signature's orphan.

    Reapability is decided by **lock liveness, not elapsed time** (#379). Each
    active build holds an exclusive ``flock`` on ``<staging>/.build.lock`` for its
    whole duration; the reaper tries to take that same lock non-blocking:

    - lock held by a live builder (``BlockingIOError``/``EWOULDBLOCK``) → **skip**,
      so an actively-building base is never reaped regardless of how long the copy
      runs (the failure mode the old 1 h duration heuristic could not avoid).
    - lock acquired, or ``.build.lock`` absent (a pre-upgrade staging dir, or a
      build killed before it locked) → a crash orphan → ``rmtree``.
    - staging dir vanished mid-check (a concurrent reap or a winning ``replace``)
      → skipped rather than fatal.
    """

    for staging in base_root.glob("*/.claude-base-*"):
        if _claude_base_staging_build_is_live(staging):
            continue
        shutil.rmtree(staging, ignore_errors=True)


def _claude_base_staging_build_is_live(staging: Path) -> bool:
    """Return whether a live builder holds ``<staging>/.build.lock``.

    Duration-independent liveness probe for :func:`_reap_stale_claude_base_staging`
    (#379): an actively-building base holds an exclusive ``flock`` the kernel
    releases only on close or process death, so a non-blocking re-lock attempt that
    is denied proves a live builder, and one that succeeds (or finds no marker)
    proves an orphan. ``flock`` works across distinct open file descriptions even
    within one process, so a separate ``open`` of a held lock is denied — which is
    what lets unit tests exercise this without a second process.
    """

    lock_path = staging / _CLAUDE_BASE_BUILD_LOCK_NAME
    try:
        lock_fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        # ``.build.lock`` (or the whole staging dir) is gone: a pre-upgrade staging
        # dir that locked nothing, a build killed before it created the marker, or a
        # dir that vanished mid-check. No live builder is protecting it → reapable.
        return False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # ``EWOULDBLOCK``/``EAGAIN`` (``BlockingIOError``): a live builder holds the
        # lock. Never reap an actively-building base, regardless of its duration.
        return True
    finally:
        os.close(lock_fd)
    return False
