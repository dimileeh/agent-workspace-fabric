"""Overlay-isolated ``~/.claude`` auth for service-created workspace stacks.

Split out of :mod:`awf.node.auth_mounts` to keep that module under the
first-party line limit. This module holds the generic overlayfs primitives
(force-copy / capability / filesystem probes, ``/proc/mounts`` parsing, the
:class:`OverlayMounter` abstraction) together with the full Claude auth
subsystem they serve: host-content signature hashing, the shared read-only base
build/reap, the per-workspace overlay mount, fallback-edit reconciliation, and
teardown. :mod:`awf.node.auth_mounts` re-exports every public name here so
``awf.node.auth_mounts.<name>`` stays the stable import surface for callers and
tests.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from awf.common.logging import get_logger
from awf.node.auth_mounts_caps import _has_cap_mknod as _has_cap_mknod
from awf.node.auth_mounts_caps import _has_cap_sys_admin as _has_cap_sys_admin
from awf.node.auth_mounts_claude_reconcile import (
    _CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED as _CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED,
)
from awf.node.auth_mounts_claude_reconcile import (
    _CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE as _CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE,
)
from awf.node.auth_mounts_claude_reconcile import (
    _CLAUDE_USAGE_HISTORY_DIRS as _CLAUDE_USAGE_HISTORY_DIRS,
)
from awf.node.auth_mounts_claude_reconcile import (
    _reconcile_fallback_edits_into_upper as _reconcile_fallback_edits_into_upper,
)
from awf.node.auth_mounts_overlay import _PROC_MOUNTS as _PROC_MOUNTS
from awf.node.auth_mounts_overlay import (
    _overlay_lowerdir_from_proc_mounts as _overlay_lowerdir_from_proc_mounts,
)
from awf.node.auth_mounts_overlay import (
    _unescape_proc_mount_field as _unescape_proc_mount_field,
)
from awf.node.auth_mounts_overlay import iter_overlay_lowerdirs as iter_overlay_lowerdirs
from awf.node.auth_mounts_overlay_copy import (
    _legacy_path_confidently_absent as _legacy_path_confidently_absent,
)
from awf.node.auth_mounts_overlay_copy import _safe_mtime_ns as _safe_mtime_ns
from awf.node.auth_mounts_overlay_copy import _safe_overlay_copy as _safe_overlay_copy
from awf.node.auth_mounts_overlay_copy import _safe_overlay_whiteout as _safe_overlay_whiteout
from awf.node.auth_mounts_overlay_copy import _safe_stat as _safe_stat
from awf.node.compose_manager import AuthMount

_log = get_logger(__name__)

_CONTAINER_HOME = "/home/agent"
_CLAUDE_DIR_TARGET = f"{_CONTAINER_HOME}/.claude"
_CLAUDE_FILE_TARGET = f"{_CONTAINER_HOME}/.claude.json"

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
_CLAUDE_AUTH_OVERLAY_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
_CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED = "CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED"
_CLAUDE_AUTH_SHARED_BASE_FAILED = "CLAUDE_AUTH_SHARED_BASE_FAILED"
# Logged when a reused live overlay's real lowerdir could not be recovered, so the
# host-recomputed base is untrustworthy: the fallback-edit reconcile (#381) and the
# legacy-copy reap are both deferred to a later provision that can pin the true base,
# rather than reconciling against a wrong tree (which would mis-copy or drop edits).
_CLAUDE_AUTH_OVERLAY_RECONCILE_DEFERRED = "CLAUDE_AUTH_OVERLAY_RECONCILE_DEFERRED"
# Raised by ``teardown_workspace_auth_overlay`` when a process that lacks
# ``CAP_SYS_ADMIN`` (the CLI / API container) is asked to release a workspace's
# overlay it cannot see in its own mount namespace and no capable process has
# recorded a teardown yet. The overlay may still be live in the worker namespace,
# so removing the auth dir would strand the mount; GC must surface this loudly.
_CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE = "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE"
# Logged when a capable process unmounted the overlay but the ``.overlay-unmounted``
# marker write failed (ENOSPC, transient FS error). Distinct from
# ``UNMOUNT_INCAPABLE`` (a capability gap): the process here *is* capable, so this
# is a filesystem write fault, and alerting keyed on the two must not conflate them.
_CLAUDE_AUTH_OVERLAY_MARKER_WRITE_FAILED = "CLAUDE_AUTH_OVERLAY_MARKER_WRITE_FAILED"
# Marker written under ``auth/<id>/claude`` by a capability-holding process (the
# worker, or any root+CAP_SYS_ADMIN context) once it has unmounted — or verified
# the absence of — the overlay. It is the cross-namespace signal that lets a
# later capability-less GC distinguish "the worker already released this overlay"
# (safe to remove) from "still mounted elsewhere" (must not remove).
_OVERLAY_UNMOUNTED_MARKER = ".overlay-unmounted"
# Sibling marker written under ``auth/<id>/claude`` once a per-workspace legacy
# ``.claude`` copy has been materialized as a *complete* atomic copy (staging dir +
# atomic ``replace``). The #402 deletion-whiteout pass reads a base-present /
# legacy-absent file as a *confident* agent deletion and whiteouts the lower
# credential; that inference is only sound when the legacy copy is whole. Atomic
# staging guarantees completeness for copies this code newly materializes, but a
# ``.claude`` left *partial* by a pre-atomic-staging provision (an interrupted plain
# ``copytree`` that landed straight in ``.claude``) is still reused by the bare
# ``exists()`` guards with no completeness check and no host-signature invalidation —
# its never-copied files would read as deletions and whiteout still-valid credentials
# (PRRT_kwDOSJAM6s6HRNkk). Only a copy materialized through the atomic path drops this
# marker; the reconcile path forwards deletions as whiteouts only when it is present,
# and otherwise fails safe (edits are still forwarded, lower credentials stay visible).
_CLAUDE_LEGACY_COMPLETE_MARKER = ".claude-complete"
# When truthy in the worker environment, force the per-workspace copy fallback
# even where overlayfs + CAP_SYS_ADMIN are present. The bootstrap mount-propagation
# preflight sets this on hosts whose work dir cannot be made an ``rshared`` mount
# (Docker Desktop / virtiofs / grpcfuse), where a worker-mounted overlay would
# never propagate into the sibling agent container and the agent would see an
# empty ``~/.claude``. The copy fallback is correct there (no disk saving).
_CLAUDE_AUTH_FORCE_COPY_ENV = "AWF_CLAUDE_AUTH_FORCE_COPY"
# overlayfs's legacy ``mount -o`` API joins options with ``,`` and stacks lower
# layers with ``:`` inside ``lowerdir=``; neither can be escaped in that payload
# (only ``/proc/mounts`` *read-back* octal-decodes ``\054``/``\072``). A workspace
# auth path carrying either character — inherited from ``AWF_WORK_DIR`` /
# ``AWF_HOST_WORK_DIR`` — therefore cannot be expressed as an overlay mount option,
# so the overlay branch degrades to the per-workspace copy fallback on such a host.
_OVERLAY_OPTION_RESERVED_CHARS = (",", ":")
_PROC_FILESYSTEMS = Path("/proc/filesystems")
_ISOLATION_OVERLAY = "per_workspace_overlay"
_ISOLATION_COPY = "per_workspace_copy"


class OverlayUnmountUnverifiableError(RuntimeError):
    """Overlay teardown could not be verified by a capability-less process.

    Raised by :func:`teardown_workspace_auth_overlay` when the calling process
    lacks ``CAP_SYS_ADMIN`` (CLI / API container), the overlay is not visibly
    mounted in this namespace, a writable overlay ``upper`` scratch exists, and
    no capable process has yet recorded the ``.overlay-unmounted`` marker. The
    overlay may still be live in the worker's mount namespace, so the caller must
    not remove the auth dir — it surfaces this as a loud GC failure instead of a
    silent no-op reported as success.
    """

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _force_copy_isolation_requested(host_env: Mapping[str, str] | None = None) -> bool:
    """Return whether ``AWF_CLAUDE_AUTH_FORCE_COPY`` requests the copy fallback."""

    source = os.environ if host_env is None else host_env
    value = source.get(_CLAUDE_AUTH_FORCE_COPY_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def force_copy_isolation_requested(host_env: Mapping[str, str] | None = None) -> bool:
    """Public API: whether ``AWF_CLAUDE_AUTH_FORCE_COPY`` requests the copy fallback.

    A stable wrapper over the module-private probe so callers outside this
    package (e.g. ``service.provider_readiness``) depend on a public symbol
    rather than coupling to the underscore-prefixed implementation detail.
    """

    return _force_copy_isolation_requested(host_env)


def _overlay_filesystem_available(proc_filesystems: Path = _PROC_FILESYSTEMS) -> bool:
    """Return whether the kernel advertises overlayfs in ``/proc/filesystems``."""

    try:
        contents = proc_filesystems.read_text()
    except OSError:
        return False
    return any("overlay" in line.split() for line in contents.splitlines())


def _overlay_path_has_reserved_chars(path: Path) -> bool:
    """Return whether ``path`` holds a char overlayfs's ``-o`` payload cannot encode.

    A literal ``,`` would split the ``lowerdir=..,upperdir=..,workdir=..`` option
    string into spurious options, and a literal ``:`` inside ``lowerdir`` would be
    misread as the separator between stacked lower layers — either breaking the
    mount or, worse, resolving a different lower than intended. ``mount(8)`` offers
    no escaping for these, so a path carrying one forces the copy fallback.
    """

    text = os.fspath(path)
    return any(char in text for char in _OVERLAY_OPTION_RESERVED_CHARS)


def overlay_path_has_reserved_chars(path: Path) -> bool:
    """Public API: whether ``path`` carries a char overlayfs's ``-o`` cannot encode.

    A stable wrapper over the module-private probe so callers outside this
    package (e.g. ``service.provider_readiness``, which folds the same signal
    into :func:`claude_auth_isolation_label`) depend on a public symbol rather
    than coupling to the underscore-prefixed implementation detail.
    """

    return _overlay_path_has_reserved_chars(path)


class OverlayMounter(Protocol):
    """Set up and tear down the per-workspace ``~/.claude`` overlay mount.

    Injected so unit tests can exercise the overlay/fallback/teardown branches
    without root or a real overlayfs kernel mount.
    """

    def supported(self) -> bool:
        """Return whether overlayfs can be mounted on this host."""
        ...  # pragma: no cover - Protocol method declaration only.

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        """Mount the overlay at ``merged``; raise on failure."""
        ...  # pragma: no cover - Protocol method declaration only.

    def unmount(self, target: Path) -> None:
        """Unmount the overlay at ``target``; raise on failure."""
        ...  # pragma: no cover - Protocol method declaration only.

    def is_mounted(self, target: Path) -> bool:
        """Return whether ``target`` is currently an overlay mountpoint."""
        ...  # pragma: no cover - Protocol method declaration only.

    def active_lowerdir(self, merged: Path) -> Path | None:
        """Return the ``lowerdir`` the overlay live-mounted at ``merged`` uses.

        ``None`` when ``merged`` is not a live overlay or its lowerdir cannot be
        determined. Lets a retry that reuses a live mount pin the surviving upper
        to the base it is *actually* mounted against rather than a recomputed guess.
        """
        ...  # pragma: no cover - Protocol method declaration only.


@dataclass(frozen=True)
class _SubprocessOverlayMounter:
    """Default :class:`OverlayMounter` backed by ``mount(8)``/``umount(8)``."""

    timeout_seconds: float = 30.0
    proc_mounts: Path = _PROC_MOUNTS

    def supported(self) -> bool:
        # An operator/bootstrap force-copy request wins over real overlayfs
        # capability: on a host whose work dir cannot be made an ``rshared``
        # mount the overlay would never reach the agent container, so the copy
        # fallback is the only correct posture there.
        if _force_copy_isolation_requested():
            return False
        return _overlay_filesystem_available() and _has_cap_sys_admin()

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        options = f"lowerdir={lowerdir},upperdir={upperdir},workdir={workdir}"
        subprocess.run(
            ["mount", "-t", "overlay", "overlay", "-o", options, str(merged)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

    def unmount(self, target: Path) -> None:
        subprocess.run(
            ["umount", str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

    def is_mounted(self, target: Path) -> bool:
        return os.path.ismount(target)

    def active_lowerdir(self, merged: Path) -> Path | None:
        return _overlay_lowerdir_from_proc_mounts(self.proc_mounts, merged)


def default_overlay_mounter() -> OverlayMounter:
    """Return the production overlay mounter (real ``mount``/``umount``)."""

    return _SubprocessOverlayMounter()


def claude_auth_isolation_label(
    *,
    overlay_filesystem_available: Callable[[], bool] | None = None,
    force_copy_requested: Callable[[], bool] | None = None,
    overlay_path_unsupported: Callable[[], bool] | None = None,
) -> str:
    """Return the isolation posture label for Claude file auth on this host.

    ``per_workspace_overlay`` when overlayfs is usable (shared read-only base +
    per-workspace writable upper), else ``per_workspace_copy`` (full per-workspace
    copy fallback).

    This describes the **worker's** posture, not the calling process's. The worker
    is the only service that provisions workspaces and mounts the overlay, and it
    alone is granted ``CAP_SYS_ADMIN`` (see ``docker/compose/local-service.yml``);
    readiness, however, is usually collected from the API/status/MCP path, which
    deliberately runs without that capability. Probing the *caller's* own
    ``CAP_SYS_ADMIN`` there would surface ``per_workspace_copy`` even though the
    worker uses ``per_workspace_overlay``, so the label keys off kernel overlayfs
    availability — the one host fact shared across services — and treats the
    worker's ``CAP_SYS_ADMIN`` as the deployment invariant it is. Best effort: an
    individual mount can still fall back to copy, which keeps provisioning correct
    either way.

    An ``AWF_CLAUDE_AUTH_FORCE_COPY`` request wins over real overlayfs capability,
    exactly as it does in ``_SubprocessOverlayMounter.supported`` — on a
    non-propagating host bootstrap sets this so the worker provisions with the copy
    fallback even while overlayfs stays advertised. The label must fold in the same
    signal or readiness/status would report ``per_workspace_overlay`` while the
    worker actually uses per-workspace copies, misstating the isolation/disk posture.

    ``overlay_path_unsupported`` folds in the same deterministic, host-level copy
    fallback that ``_prepare_claude_overlay_mount`` takes when the workspace auth
    path (inherited from ``AWF_WORK_DIR`` / ``AWF_HOST_WORK_DIR``) carries a ``,``
    or ``:`` that overlayfs's unescapable ``-o`` payload cannot encode. On such a
    host *every* overlay mount degrades to the per-workspace copy, so — like the
    force-copy signal — the label must report ``per_workspace_copy`` rather than
    overstate overlay isolation. Defaults to a no-op (no path-derived signal) so
    callers without a work-dir context keep the kernel-availability-only behavior.
    """

    probe = overlay_filesystem_available or _overlay_filesystem_available
    force_copy = force_copy_requested or _force_copy_isolation_requested
    path_unsupported = overlay_path_unsupported or (lambda: False)
    if force_copy():
        return _ISOLATION_COPY
    if not probe():
        return _ISOLATION_COPY
    if path_unsupported():
        return _ISOLATION_COPY
    return _ISOLATION_OVERLAY


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


@dataclass(frozen=True)
class _ClaudeAuthResult:
    """Claude auth mounts plus chown routing for the overlay branch.

    ``chown_exempt_sources`` lists mount sources the generic rw-chown walk must
    skip (the live overlay ``merged`` mount). ``extra_chown_paths`` lists the
    writable overlay scratch dirs (``upper``/``work``) chowned in their place.
    """

    mounts: tuple[AuthMount, ...]
    chown_exempt_sources: frozenset[str] = frozenset()
    extra_chown_paths: tuple[Path, ...] = ()


def _overlay_upper_has_data(upper: Path) -> bool:
    """True when a surviving overlay ``upper`` carries agent mutations.

    The first overlay attempt creates an empty ``upper`` before mounting; a failed
    mount leaves that empty dir on disk while provisioning falls back to the legacy
    full copy (which the agent then mutates). An empty leftover therefore must not
    masquerade as a real overlay and shadow the mutated legacy copy. Only an
    ``upper`` with at least one entry — the writable layer of a previously mounted
    overlay — counts as real overlay data.
    """

    try:
        return any(upper.iterdir())
    except OSError:
        # Missing dir (a pure legacy/no-overlay workspace) or an unreadable one:
        # treat as no overlay data so the legacy-copy guard stays in force.
        return False


def _write_legacy_complete_marker(target_root: Path) -> None:
    """Drop the per-workspace ``.claude`` completeness marker (best-effort).

    Called only *after* an atomic legacy-copy materialization, so the marker's
    presence proves the copy is whole (see :data:`_CLAUDE_LEGACY_COMPLETE_MARKER`).
    Written strictly after the ``replace`` so a crash between the rename and this
    write yields a complete copy with *no* marker — the reconcile path then skips the
    destructive whiteout pass (over-conservative but safe); the inverse, a marker over
    a partial copy, is impossible. A failed write (ENOSPC, transient FS error) is
    swallowed for the same reason: a missing marker only forgoes whiteouting confident
    deletions, never blocks provisioning and never hides a credential.
    """

    with contextlib.suppress(OSError):
        (target_root / _CLAUDE_LEGACY_COMPLETE_MARKER).touch()


def _prepare_isolated_claude_auth(
    *,
    host_home: Path,
    target_root: Path,
    work_dir: Path,
    overlay_mounter: OverlayMounter,
    suppressed_targets: Collection[str] = (),
    workspace_owner_uid: int | None = None,
    workspace_owner_gid: int | None = None,
) -> _ClaudeAuthResult:
    """Seed per-workspace Claude auth without sharing writable host files.

    Prefers a shared read-only base + per-workspace writable overlay (no
    ~1.7 GB per-workspace copy); falls back to the legacy full copy when
    overlayfs is unavailable so provisioning never hard-fails.
    """

    mounts: list[AuthMount] = []
    chown_exempt: set[str] = set()
    extra_chown: list[Path] = []
    source_dir = host_home / ".claude"
    legacy_claude_copy = target_root / ".claude"
    if _CLAUDE_DIR_TARGET not in suppressed_targets and source_dir.is_dir():
        # A prior legacy/pre-upgrade provision (overlay unsupported then) may have
        # left a per-workspace ``.claude`` copy carrying the agent's auth/config
        # mutations. The legacy branch deliberately reuses such a copy (``if not
        # target_dir.exists()``) to preserve retry state; mounting a fresh
        # shared-base overlay over it would silently drop those mutations (empty
        # ``upper``, lower seeded from the current host). So normally only reach
        # for the overlay when no legacy copy exists — otherwise keep using that
        # copy.
        #
        # Exception: a surviving *non-empty* overlay ``upper`` overrides that guard.
        # A transient remount failure preserves ``upper``/``work`` on disk but still
        # degrades this provision to a *fresh* legacy copy (no mutations). Without
        # this override the next retry would see that fresh copy, skip the overlay
        # forever, and strand the agent's real mutations in the unremounted
        # ``upper``. A non-empty ``upper`` exists only for overlay-backed workspaces
        # (a pure legacy copy never has one), so its presence safely distinguishes
        # "remount the surviving overlay" from "preserve a real legacy copy". The
        # overlay mount, when it succeeds, takes precedence over the stale legacy
        # copy.
        #
        # The ``upper`` must be *non-empty* to override: the first overlay attempt
        # creates an empty ``upper`` before the mount, and if that mount fails the
        # empty dir is left behind while the fallback legacy copy is what the agent
        # actually mutates. Treating that empty leftover as a real overlay would
        # mount it over a fresh shared base and silently shadow the mutated legacy
        # copy. So only a ``upper`` carrying real overlay data bypasses the guard.
        overlay_upper = target_root / "upper"
        overlay = (
            None
            if legacy_claude_copy.exists() and not _overlay_upper_has_data(overlay_upper)
            else _prepare_claude_overlay_mount(
                host_home=host_home,
                claude_root=target_root,
                work_dir=work_dir,
                overlay_mounter=overlay_mounter,
                workspace_owner_uid=workspace_owner_uid,
                workspace_owner_gid=workspace_owner_gid,
            )
        )
        if overlay is not None:
            mount, upper, work, base = overlay
            mounts.append(mount)
            chown_exempt.add(mount.source)
            extra_chown.extend((upper, work))
            # A surviving non-empty ``upper`` let the overlay win over a stale
            # legacy full copy (the ``_overlay_upper_has_data`` override above):
            # that copy was created by a transient-failure fallback and is now
            # unmounted dead weight (~1.7 GB) whose contents the live overlay
            # intentionally supersedes. Remove it so it does not leak on disk for
            # the workspace's lifetime. ``ignore_errors`` keeps a stuck reap from
            # failing provisioning — a later provision retries the cleanup.
            #
            # Before reaping it, reconcile any *fallback-era edits* it carries forward
            # into the overlay (#381): a prior provision that degraded to this legacy
            # copy may have mutated Claude auth/config there, and the remounted
            # overlay (``merged`` = upper over ``base``) would otherwise shadow then
            # drop those edits. Reconciliation copies only the strictly-newer legacy
            # edits forward (``upper`` wins ties), so the fresh-copy baseline — which
            # matches ``base`` — is left untouched and the overlay's disk savings hold.
            # The copies route *through* the live ``merged`` mount (``mount.source``),
            # not straight into ``upper``: the overlay is already mounted here, and
            # writing into a live overlay's upper tree directly is undefined behavior
            # that can leave the edits invisible/stale to the agent reading ``merged``.
            if legacy_claude_copy.exists():
                if base is None:
                    # ``_prepare_claude_overlay_mount`` reused a live overlay but
                    # could not recover the base it is actually mounted against, so
                    # there is no trustworthy tree to reconcile the legacy copy's
                    # fallback edits against. Reconciling against a host guess could
                    # copy baseline noise into the live overlay or drop a real edit,
                    # and reaping the legacy copy afterwards would lose those edits
                    # for good. Defer both: leave the legacy copy on disk so a later
                    # provision that recovers/pins the true base can reconcile and
                    # reap it. Disk leak (~1.7 GB) is the recoverable cost; lost edits
                    # are not.
                    _log.info(
                        "claude_auth_overlay_reconcile_deferred",
                        reason_code=_CLAUDE_AUTH_OVERLAY_RECONCILE_DEFERRED,
                        workspace_auth_root=str(target_root),
                    )
                else:
                    # Forward fallback-era *deletions* as whiteouts only when this
                    # legacy copy is proven complete (the completeness marker is
                    # present): only then is a base-present / legacy-absent file a
                    # confident agent deletion rather than a never-copied file of a
                    # partial pre-atomic-staging copy. A partial copy reused by the
                    # ``exists()`` guard above carries no marker, so its absences are
                    # left visible. Edits are always forwarded regardless — they only
                    # add/overwrite under ``upper`` and so can never hide a lower
                    # credential.
                    legacy_complete = (target_root / _CLAUDE_LEGACY_COMPLETE_MARKER).exists()
                    _reconcile_fallback_edits_into_upper(
                        legacy=legacy_claude_copy,
                        merged=Path(mount.source),
                        upper=upper,
                        base=base,
                        host_claude=source_dir,
                        forward_deletions=legacy_complete,
                    )
                    shutil.rmtree(legacy_claude_copy, ignore_errors=True)
                    # Reap the completeness marker with the copy it vouches for. It
                    # lives *beside* the legacy tree (``target_root``), so ``rmtree``
                    # of ``.claude`` leaves it dangling otherwise. Its invariant is
                    # "present ⟹ *this* ``.claude`` copy is provably complete"; a
                    # stale marker over a since-reaped copy would falsely vouch for a
                    # later partial ``.claude`` that lands here without the atomic
                    # write path (e.g. a concurrent older-code provision), keeping
                    # ``forward_deletions`` true so the whiteout pass could hide
                    # still-valid lower credentials. Removing it restores the
                    # safe default (a missing marker forgoes the destructive pass).
                    (target_root / _CLAUDE_LEGACY_COMPLETE_MARKER).unlink(missing_ok=True)
        else:
            target_dir = legacy_claude_copy
            target_root.mkdir(parents=True, exist_ok=True)
            if not target_dir.exists():
                # Materialize the legacy copy atomically: ``copytree`` into a sibling
                # staging dir, then ``replace`` it into ``.claude`` only once the copy
                # is complete. A plain ``copytree`` straight into ``.claude`` that is
                # interrupted (crash, SIGKILL, OSError mid-copy) leaves a *partial*
                # tree, and the ``not target_dir.exists()`` reuse guard above would then
                # adopt it as the authoritative legacy copy on the next provision. Its
                # never-copied files would read as confident agent deletions to the #402
                # whiteout pass (:func:`_forward_fallback_deletions_as_whiteouts`), which
                # would synthesize overlayfs whiteouts that *hide still-valid lower
                # credentials* from ``merged`` — the exact credential-hiding the
                # reconcile design forbids. Staging + an atomic rename guarantees
                # ``.claude`` only ever exists as a *complete* copy; an interrupted
                # attempt leaves only the discardable staging dir (reclaimed by the
                # ``finally``, or — on a hard kill — by the per-workspace teardown that
                # reaps ``target_root`` wholesale). Mirrors the shared-base build's
                # staging pattern in :func:`_ensure_shared_claude_base`.
                staging = Path(tempfile.mkdtemp(prefix=".claude-legacy-", dir=target_root))
                staged_copy = staging / ".claude"
                try:
                    shutil.copytree(
                        source_dir,
                        staged_copy,
                        ignore=shutil.ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS),
                        ignore_dangling_symlinks=True,
                    )
                    try:
                        staged_copy.replace(target_dir)
                    except OSError:
                        # A concurrent provision of the same workspace won the race and
                        # already materialized ``.claude`` (renaming onto the now
                        # non-empty dir fails): reuse theirs. Any other failure leaves
                        # ``.claude`` absent — re-raise so the caller surfaces it rather
                        # than mounting a missing source.
                        if not target_dir.exists():
                            raise
                        staged_replace_won = False
                    else:
                        staged_replace_won = True
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
                # Drop the completeness marker only when *this* provision's atomic
                # ``replace`` landed the copy — then ``target_dir`` is provably the whole
                # staged tree we just built, and a later overlay-reconcile may treat its
                # absences as confident agent deletions. When the ``replace`` instead lost
                # the race, the winner owns its copy and marks it itself once *its* atomic
                # rename lands; we must not vouch for a tree we did not complete, because
                # the winner need not be this atomic path — a concurrent *older*
                # pre-atomic-staging provision may have left a *partial* ``.claude`` that
                # our rename then failed onto. A marker over a partial copy would make the
                # reconcile whiteout still-valid lower credentials; a missing marker is the
                # safe direction (the reconcile skips that destructive pass).
                if staged_replace_won:
                    _write_legacy_complete_marker(target_root)
            mounts.append(
                AuthMount(
                    source=str(target_dir),
                    target=_CLAUDE_DIR_TARGET,
                    mode="rw",
                )
            )

    source_file = host_home / ".claude.json"
    target_file = target_root / ".claude.json"
    if _CLAUDE_FILE_TARGET not in suppressed_targets and source_file.is_file():
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_file.exists():
            shutil.copy2(source_file, target_file)
        mounts.append(
            AuthMount(
                source=str(target_file),
                target=_CLAUDE_FILE_TARGET,
                mode="rw",
            )
        )

    return _ClaudeAuthResult(
        mounts=tuple(mounts),
        chown_exempt_sources=frozenset(chown_exempt),
        extra_chown_paths=tuple(extra_chown),
    )


def _pinned_overlay_base(work_dir: Path, sig_marker: Path) -> Path | None:
    """Return the base an existing overlay ``upper``/``work`` were built against.

    Reads the ``base.signature`` marker written when the overlay was first
    created and resolves it back to the shared base dir. Returns ``None`` when no
    marker exists (a pre-pin overlay, or none at all) or the recorded base is no
    longer on disk — the caller then rebuilds from the current host. The recorded
    base normally persists because shared bases are immutable and old signatures
    are left intact for exactly this case (a still-relevant lowerdir).
    """

    try:
        signature = sig_marker.read_text().strip()
    except OSError:
        return None
    base = _shared_claude_base_dir(work_dir, signature)
    return base if base.is_dir() else None


def _record_overlay_base_pin(sig_marker: Path, signature: str, claude_root: Path) -> None:
    """Persist the base-signature pin for an already-established overlay.

    Records which shared base the live ``upper``/``work`` belong to so a later
    teardown + remount pins them to that exact base instead of recomputing it
    from a since-changed host ``~/.claude`` (which would trip an upper/base
    mismatch whose failure path ``rmtree``s the agent's overlay mutations). A
    failed write only forfeits the pin hint for a future teardown+retry (which
    then recomputes the base from the host); the overlay is already live and
    correct for this provision, so never hard-fail provisioning on it.
    """

    try:
        sig_marker.write_text(signature)
    except OSError as exc:
        # The overlay is already mounted and correct for this provision; only the
        # base-signature pin write failed. Use a distinct reason code so this
        # harmless metadata-write failure is not conflated with an actual
        # mount-unavailable event when operators grep the logs.
        _log.warning(
            "claude_auth_overlay_base_pin_write_failed",
            reason_code=_CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )


def _live_overlay_pin_signature(
    overlay_mounter: OverlayMounter, merged: Path, work_dir: Path
) -> str | None:
    """Resolve the base signature to pin for a *reused* live overlay.

    A retry reaches the live-mount reuse branch when the prior provision was
    killed after ``mount()`` succeeded but before it recorded ``base.signature``:
    ``upper`` exists yet the marker is missing. The signature recomputed from the
    current host is only correct if ``~/.claude`` is unchanged — if the operator
    edited it since the kill, that value names a *different* base than the one this
    still-live overlay is actually mounted against, and persisting it would later
    remount the surviving upper over the wrong base (the exact mismatch whose
    failure path ``rmtree``s the agent's mutations). So recover the ``lowerdir``
    the live mount really uses and map it back to its shared-base signature.

    Returns ``None`` — meaning write no pin, leaving a later teardown+retry to
    recompute from the host — when the live lowerdir cannot be recovered or does
    not resolve to a shared base under ``work_dir``: no pin is safer than a guess.
    """

    live_lowerdir = overlay_mounter.active_lowerdir(merged)
    if live_lowerdir is None:
        return None
    signature = live_lowerdir.parent.name
    shared_base = _shared_claude_base_dir(work_dir, signature)
    # Compare in *resolved* form: ``active_lowerdir`` reads the live lowerdir from
    # ``/proc/mounts`` in the kernel-resolved form (the kernel follows symlinks when
    # recording mount paths), while ``_shared_claude_base_dir`` is built from a
    # ``work_dir`` that only had ``expanduser()`` applied. When ``AWF_WORK_DIR`` is
    # reached via a symlink or bind-mount alias the two string forms diverge, so a
    # valid shared base would fail this equality check and no pin would be recorded —
    # leaving a later teardown+remount to recompute against a since-changed host. The
    # GC protection code resolves both sides for exactly this reason.
    if shared_base.resolve(strict=False) != live_lowerdir.resolve(strict=False):
        return None
    # The live overlay keeps serving off kernel-held inodes even after its lowerdir
    # *path* is removed or renamed on the host, and ``resolve(strict=False)`` above
    # matches such a stale ``/proc/mounts`` path without proving the dir still exists.
    # Returning the signature anyway would have the caller realign ``base`` to that
    # vanished tree, and ``_reconcile_fallback_edits_into_upper`` — which reads a
    # missing ``base[rel]`` as "legacy is newer than base" — would then copy the whole
    # legacy tree into the live overlay. Require the base to still be a real directory,
    # mirroring ``_pinned_overlay_base``'s ``is_dir`` guard; otherwise return ``None`` so
    # the caller defers the reconcile (and the legacy reap) and writes no pin.
    if not shared_base.is_dir():
        return None
    return signature


def _prepare_claude_overlay_mount(
    *,
    host_home: Path,
    claude_root: Path,
    work_dir: Path,
    overlay_mounter: OverlayMounter,
    workspace_owner_uid: int | None,
    workspace_owner_gid: int | None,
) -> tuple[AuthMount, Path, Path, Path | None] | None:
    """Mount a shared-base + per-workspace overlay at ``<claude_root>/merged``.

    Returns ``(merged_mount, upper, work, base)`` on success, or ``None`` to signal
    the caller should fall back to the legacy full copy (overlay unsupported or the
    mount failed). ``base`` is the resolved ``lowerdir`` (pinned, freshly built, or
    recovered from a live/raced mount) — the caller needs it to reconcile any
    fallback-era legacy edits into ``upper`` before reaping the legacy copy (#381).
    The shared base is built once per host and reused.

    ``base`` is ``None`` only when a *live overlay was reused* but its real lowerdir
    could not be recovered from the mount (``_live_overlay_pin_signature`` returned
    ``None``): the host-recomputed tree we hold is not the one the overlay is
    actually mounted against, so the caller must **not** reconcile fallback edits
    against it (a wrong base copies baseline noise into the live overlay or skips a
    real edit before the legacy copy is reaped). The caller defers both the
    reconcile and the legacy reap to a later provision that can recover/pin the base.
    """

    if not overlay_mounter.supported():
        _log.info(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            reason="overlayfs_unsupported",
        )
        return None

    # The overlay paths feed an unescapable ``mount -o lowerdir=..,upperdir=..,
    # workdir=..`` payload: ``base`` lives under ``work_dir`` and ``upper``/``work``
    # under ``claude_root``. If either carries a ``,`` or ``:`` (from a comma/colon
    # in ``AWF_WORK_DIR``/``AWF_HOST_WORK_DIR``) the option string would tear apart
    # or be misread as an extra lower layer, so the mount cannot faithfully express
    # these paths. Degrade to the per-workspace copy fallback — the same posture a
    # force-copy host takes — rather than attempt a broken or ambiguous mount.
    if _overlay_path_has_reserved_chars(work_dir) or _overlay_path_has_reserved_chars(claude_root):
        _log.info(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            reason="overlay_path_reserved_chars",
        )
        return None

    upper = claude_root / "upper"
    work = claude_root / "work"
    merged = claude_root / "merged"
    sig_marker = claude_root / "base.signature"

    # Pin the lowerdir for retries over an existing overlay. If ``upper``/``work``
    # survived a torn-down mount (e.g. a host reboot) they must be remounted over
    # the *original* base they were created against. Recomputing the base from a
    # since-changed host ``~/.claude`` would either expose the changed host config
    # through the surviving upper or trip an overlayfs upper/work mismatch — and
    # that failure path ``rmtree``s upper/work, destroying the agent's Claude
    # mutations. ``base.signature`` records which base each upper belongs to.
    base = _pinned_overlay_base(work_dir, sig_marker) if upper.exists() else None
    fresh_signature: str | None = None
    if base is None:
        fresh_signature = _host_claude_signature(host_home)
        try:
            base = _ensure_shared_claude_base(
                host_home=host_home,
                work_dir=work_dir,
                signature=fresh_signature,
                workspace_owner_uid=workspace_owner_uid,
                workspace_owner_gid=workspace_owner_gid,
            )
        except OSError as exc:
            # Building the shared base (copytree/chown) can fail with OSError (disk
            # full, permissions). Degrade to the legacy full copy rather than
            # hard-failing provisioning — the same resilience contract honored when
            # overlayfs is unsupported or the mount itself fails.
            _log.warning(
                "claude_auth_shared_base_failed",
                reason_code=_CLAUDE_AUTH_SHARED_BASE_FAILED,
                workspace_auth_root=str(claude_root),
                error=str(exc),
            )
            return None
    try:
        for directory in (upper, work, merged):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Creating the per-workspace scratch dirs can fail with OSError (disk full
        # after the base copytree just consumed space, permissions). Degrade to the
        # legacy full copy rather than hard-failing provisioning — the same
        # resilience contract honored when the base build or the mount itself fails.
        # ``upper``/``work`` are left intact: a retry path may carry the agent's
        # overlay mutations there, which a teardown would destroy.
        _log.warning(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        return None

    if overlay_mounter.is_mounted(merged):
        # Idempotent retry: a prior provision already mounted this overlay (e.g.
        # the auth dir survived a failed stack launch). Reuse the live mount
        # instead of remounting onto a busy mountpoint — a second mount would
        # raise EBUSY and the cleanup below would ``rmtree`` the writable
        # ``upper``/``work`` layers, destroying the agent's overlay data and
        # forcing a needless full-copy fallback.
        if fresh_signature is not None:
            # The live overlay survived from a prior provision that was killed
            # after ``mount()`` succeeded but before it recorded the pin (or the
            # marker write failed): ``upper`` exists yet ``base.signature`` is
            # missing, so ``_pinned_overlay_base`` returned None and a fresh
            # signature was computed above. That fresh value matches the live
            # overlay's base only if the host is unchanged; if the operator edited
            # ``~/.claude`` since the kill it names a different base, and pinning it
            # would later remount the surviving upper over the wrong base. So pin
            # the base the live mount is *actually* using — recovered from the mount
            # itself — and write nothing if it cannot be recovered (a later
            # teardown + retry then recomputes), never a guess from the changed host.
            pin_signature = _live_overlay_pin_signature(overlay_mounter, merged, work_dir)
            if pin_signature is not None:
                _record_overlay_base_pin(sig_marker, pin_signature, claude_root)
                # ``base`` was recomputed from the *current* host above (no pin
                # existed), but the live overlay is mounted against whatever base it
                # was created with — a different one if ``~/.claude`` changed since.
                # The caller reconciles fallback-era legacy edits against this
                # returned ``base``; comparing against the host guess would miss real
                # edits or copy baseline files into ``upper``. Realign it to the
                # lowerdir the mount actually uses, just recovered for the pin.
                base = _shared_claude_base_dir(work_dir, pin_signature)
            else:
                # The live mount's real lowerdir could not be recovered, so the
                # host-recomputed ``base`` is not the tree the overlay actually uses.
                # Returning it would make the caller reconcile fallback edits against
                # the wrong base — copying baseline noise into the live overlay or
                # skipping a real edit before the legacy copy is reaped. Drop ``base``
                # to ``None`` so the caller defers the reconcile (and the legacy reap)
                # to a later provision that can recover/pin the true base.
                base = None
        return (
            AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
            upper,
            work,
            base,
        )

    try:
        overlay_mounter.mount(lowerdir=base, upperdir=upper, workdir=work, merged=merged)
    except (OSError, subprocess.SubprocessError) as exc:
        if overlay_mounter.is_mounted(merged):
            # A concurrent provision won the mount race in the window between the
            # ``is_mounted`` pre-check above (then false) and this attempt, so our
            # ``mount`` failed (EBUSY) onto a now-live overlay. Tearing down
            # ``upper``/``work`` here would destroy the winner's writable layer
            # while the overlay stays mounted; reuse the live mount instead,
            # exactly as the idempotent-retry pre-check does.
            if fresh_signature is not None:
                # Mirror the idempotent-retry reuse branch: the live overlay was
                # mounted by the racing winner, which may have been killed before
                # its post-mount pin write, leaving ``base.signature`` missing
                # (so ``_pinned_overlay_base`` returned None and a fresh signature
                # was computed above). That fresh value matches the live overlay's
                # base only if the host is unchanged; pinning it after an operator
                # ``~/.claude`` edit would later remount the surviving upper over
                # the wrong base. Pin the base the live mount is *actually* using —
                # recovered from the mount — and write nothing if it cannot be
                # recovered, never a guess from the changed host.
                pin_signature = _live_overlay_pin_signature(overlay_mounter, merged, work_dir)
                if pin_signature is not None:
                    _record_overlay_base_pin(sig_marker, pin_signature, claude_root)
                    # As in the idempotent-retry branch: ``base`` was recomputed from
                    # the current host, but the racing winner's live overlay runs
                    # against the base it was mounted with. Realign the returned
                    # ``base`` to that recovered lowerdir so the caller reconciles
                    # fallback-era legacy edits against the tree the mount truly uses,
                    # not a host guess that would mis-copy or drop edits.
                    base = _shared_claude_base_dir(work_dir, pin_signature)
                else:
                    # Mirror the idempotent-retry branch: the racing winner's live
                    # lowerdir could not be recovered, so the host-recomputed ``base``
                    # is not the tree the overlay actually uses. Drop it to ``None`` so
                    # the caller skips the reconcile (and the legacy reap) rather than
                    # comparing fallback edits against a wrong base.
                    base = None
            return (
                AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
                upper,
                work,
                base,
            )
        _log.warning(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
            # ``str(CalledProcessError)`` emits only the command + return code; the
            # kernel reason (e.g. "special device overlay does not exist", "upper
            # fs does not support tmpfile") sits in ``stderr``. Forward it so an
            # operator grepping for the copy-fallback degrade sees *why*.
            stderr=getattr(exc, "stderr", None),
        )
        # Remove only the unused ``merged`` mountpoint. ``upper``/``work`` are left
        # intact: a normal teardown leaves the agent's overlay mutations in
        # ``upper`` on disk, and a retry that fails to remount here (a transient
        # error — the pinned lowerdir already rules out the upper/base mismatch)
        # must not wipe them. We degrade to the legacy full copy for now; a later
        # provision can pin the surviving ``upper`` and remount it, recovering the
        # mutations. This matches the scratch-dir OSError path above. No signature
        # marker is undone here: it is now written only *after* a successful mount
        # (below), so a failed — or never-reached — mount never leaves a pin.
        shutil.rmtree(merged, ignore_errors=True)
        return None

    if fresh_signature is not None:
        # Record the base signature only now that the overlay is actually
        # established, so a later retry over this upper/work pins to this exact
        # base instead of recomputing it from a changed host. Writing it before the
        # mount risked a crash in the window between the write and a successful
        # mount: the next retry would see the empty ``upper`` plus a stale pin and
        # reuse the old base for what is really a fresh provision, mounting stale
        # Claude auth/config if the operator changed the host ``~/.claude`` in
        # between. A post-mount write means a pin exists only for an overlay that
        # genuinely ran. (Pinned retries leave ``fresh_signature`` None — the
        # marker already exists and was validated by the prior successful mount.)
        _record_overlay_base_pin(sig_marker, fresh_signature, claude_root)

    return (
        AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
        upper,
        work,
        base,
    )


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


def _write_overlay_unmounted_marker(claude_root: Path, marker: Path) -> None:
    """Record that a capable process verified this overlay's teardown.

    The ``.overlay-unmounted`` marker is the cross-namespace proof that lets a
    later capability-less GC distinguish "worker already released this overlay"
    from "still mounted in another namespace". Skipped entirely when the auth
    dir does not exist (a workspace that was never provisioned), to avoid
    materializing an empty tree.

    A marker write can fail (ENOSPC, a transient FS error). The worker's
    terminal-runtime-release sweep runs *once* per workspace, so — contrary to a
    "best-effort, the next sweep re-writes it" assumption — no later capable
    sweep re-records it. Lost silently, the marker leaves a capability-less GC
    seeing ``upper`` without a marker, treating the (already gone) overlay as
    unverifiable, and skipping the auth-dir delete indefinitely: a pure leak.
    Since a capable caller reaches here only after the mount is provably gone,
    fall back to removing the overlay scratch (``upper``/``work``) directly —
    that clears the very signal GC keys off, so GC can reclaim the auth dir even
    without the marker. The fallback is itself best-effort (GC's loud-failure
    path remains the net if even removal fails).
    """

    if not claude_root.exists():
        return
    try:
        marker.write_text("")
        return
    except OSError as exc:
        _log.warning(
            "claude_auth_overlay_unmounted_marker_write_failed",
            reason_code=_CLAUDE_AUTH_OVERLAY_MARKER_WRITE_FAILED,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
    for scratch in ("upper", "work"):
        shutil.rmtree(claude_root / scratch, ignore_errors=True)


def teardown_workspace_auth_overlay(
    *,
    work_dir: Path,
    workspace_id: str,
    overlay_mounter: OverlayMounter | None = None,
    capability_probe: Callable[[], bool] | None = None,
) -> None:
    """Unmount a workspace's Claude overlay before its auth dir is removed.

    Unmount-before-remove: a busy overlay mount makes ``rmtree`` fail with
    ``EBUSY``, which is exactly the class of leak GC cannot clean up. This only
    unmounts (GC owns removal); GC failures are *not* logged here — every caller
    logs with its own context and the shared ``CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED``
    reason code, so logging here too would double-record.

    The overlay is created by the **worker** (it alone holds ``CAP_SYS_ADMIN``
    and shares the agent container's mount namespace). ``awf service gc`` runs in
    the API container / host CLI, which holds neither — there the worker's mount
    is *invisible*, so ``is_mounted`` is ``False`` even while the overlay is live.
    Treating that as a no-op and removing the auth dir would strand the mount and
    its ``upper`` inodes. The teardown therefore branches on capability
    (``capability_probe``, defaulting to :func:`_has_cap_sys_admin` so tests need
    no real caps):

    - **mounted (visible here):** unmount (raises loud on a genuine umount
      failure, unchanged) and record the ``.overlay-unmounted`` marker.
    - **not mounted + capable** (worker / root+SYS_ADMIN): a capable process has
      verified there is nothing to release — record the marker and return.
    - **not mounted + incapable** (CLI / API): if a writable overlay ``upper``
      exists and no marker is present, the worker has not yet released this
      overlay; raise :class:`OverlayUnmountUnverifiableError` so GC fails loudly
      instead of stranding a live mount. If the marker exists (worker already
      released) or there is no overlay scratch (legacy full-copy workspace),
      return a no-op.
    """

    mounter = overlay_mounter or default_overlay_mounter()
    probe = capability_probe or _has_cap_sys_admin
    claude_root = work_dir.expanduser() / "auth" / workspace_id / "claude"
    merged = claude_root / "merged"
    marker = claude_root / _OVERLAY_UNMOUNTED_MARKER
    upper = claude_root / "upper"

    if mounter.is_mounted(merged):
        mounter.unmount(merged)
        _write_overlay_unmounted_marker(claude_root, marker)
        return

    if probe():
        # A capable process (the worker) sees the real mount namespace: nothing
        # is mounted, so teardown is verified. Record the marker so a later
        # capability-less GC knows this overlay was released here -- but only when
        # an overlay scratch (``upper``) actually existed. Copy-fallback
        # workspaces (``AWF_CLAUDE_AUTH_FORCE_COPY``) never built one, and GC's
        # capability-less path consults the marker only when ``upper`` exists, so
        # writing it for a copy workspace is meaningless on-disk noise that only
        # confuses debugging.
        if upper.exists():
            _write_overlay_unmounted_marker(claude_root, marker)
        return

    if upper.exists() and not marker.exists():
        # Capability-less and the overlay's writable layer exists but no capable
        # process has recorded a teardown: the worker may still hold the mount.
        raise OverlayUnmountUnverifiableError(
            reason_code=_CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE,
            message=(
                "cannot verify Claude auth overlay teardown without CAP_SYS_ADMIN "
                f"for workspace {workspace_id}; the worker releases terminal "
                "overlays in its own mount namespace"
            ),
        )
