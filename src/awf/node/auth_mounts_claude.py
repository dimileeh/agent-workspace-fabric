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
from awf.node.auth_mounts_claude_base import (
    _CLAUDE_AUTH_SHARED_BASE_FAILED as _CLAUDE_AUTH_SHARED_BASE_FAILED,
)
from awf.node.auth_mounts_claude_base import (
    _CLAUDE_BASE_BUILD_LOCK_NAME as _CLAUDE_BASE_BUILD_LOCK_NAME,
)
from awf.node.auth_mounts_claude_base import _CLAUDE_BASE_DIRNAME as _CLAUDE_BASE_DIRNAME
from awf.node.auth_mounts_claude_base import _SHARED_AUTH_DIRNAME as _SHARED_AUTH_DIRNAME
from awf.node.auth_mounts_claude_base import _chown_tree as _chown_tree
from awf.node.auth_mounts_claude_base import (
    _claude_base_staging_build_is_live as _claude_base_staging_build_is_live,
)
from awf.node.auth_mounts_claude_base import (
    _ensure_shared_claude_base as _ensure_shared_claude_base,
)
from awf.node.auth_mounts_claude_base import _host_claude_signature as _host_claude_signature
from awf.node.auth_mounts_claude_base import (
    _reap_stale_claude_base_staging as _reap_stale_claude_base_staging,
)
from awf.node.auth_mounts_claude_base import _shared_claude_base_dir as _shared_claude_base_dir
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

# The shared read-only overlay base subsystem — its ``_shared``/``claude-base``
# dir names, the per-build ``.build.lock`` marker, and the ``CLAUDE_AUTH_SHARED_
# BASE_FAILED`` reason code — lives in :mod:`awf.node.auth_mounts_claude_base`
# and is re-imported above so ``awf.node.auth_mounts.<name>`` stays stable.
_CLAUDE_AUTH_OVERLAY_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
_CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED = "CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED"
# Logged when a surviving *non-empty* overlay ``upper`` whose base cannot be verified is
# DISCARDED and rebuilt rather than remounted over a base guessed from the current host
# (#405). The upper is unverifiable when there is no ``base.signature`` pin, or a pin that
# resolves to neither a base still on disk (:func:`_pinned_overlay_base`) nor the
# current-host signature (:func:`_pin_matches_signature`) — and no live overlay remains to
# recover the true lowerdir from. Remounting the upper over such a guessed base is a
# wrong-base correctness gap (it could expose a changed host's config/credentials through an
# upper built against a different lower). The owner ruled wrong-base-correctness WINS over
# credential-preservation here: discard the upper and rebuild ~/.claude (credentials
# included) fresh from the CURRENT host base, so from the operator's perspective there is no
# credential loss (they changed ~/.claude precisely because they refreshed something). The
# discard is deliberately LOUD via this reason code so it is auditable and never silent.
_CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT = (
    "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"
)
# Logged when a reused live overlay's real lowerdir could not be recovered, so the
# host-recomputed base is untrustworthy: the fallback-edit reconcile (#381) and the
# legacy-copy reap are both deferred to a later provision that can pin the true base,
# rather than reconciling against a wrong tree (which would mis-copy or drop edits).
_CLAUDE_AUTH_OVERLAY_RECONCILE_DEFERRED = "CLAUDE_AUTH_OVERLAY_RECONCILE_DEFERRED"
# Logged when clearing the legacy-copy completeness marker before its reap fails with a
# non-``FileNotFoundError`` ``OSError`` (readonly mount, EPERM, transient I/O). The reap
# is best-effort and the overlay is already prepared+reconciled, so a cleanup-only fault
# must not fail auth provisioning. We deliberately skip the reap too: a marker we could
# not clear over a tree the ``rmtree`` then partially removes is the credential-hiding
# state the clear-before-reap ordering exists to prevent, so the still-complete tree and
# its valid marker are both left intact for a later provision to retry.
_CLAUDE_AUTH_OVERLAY_LEGACY_REAP_DEFERRED = "CLAUDE_AUTH_OVERLAY_LEGACY_REAP_DEFERRED"
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
                    # Reap the completeness marker with the copy it vouches for, and
                    # do it *before* the (potentially multi-GB) ``rmtree`` — not after.
                    # The marker lives *beside* the legacy tree (``target_root``), so
                    # ``rmtree`` of ``.claude`` leaves it dangling. Its invariant is
                    # "present ⟹ *this* ``.claude`` copy is provably complete"; a
                    # stale marker over a since-reaped copy would falsely vouch for a
                    # later partial ``.claude`` that lands here without the atomic
                    # write path (e.g. a concurrent older-code provision), keeping
                    # ``forward_deletions`` true so the whiteout pass could hide
                    # still-valid lower credentials. Removing it restores the
                    # safe default (a missing marker forgoes the destructive pass).
                    #
                    # Order matters: a worker killed *during* the large ``rmtree`` would
                    # leave a *partially removed* legacy tree behind. If the marker were
                    # only cleared afterwards it would survive, and the next provision's
                    # ``legacy_complete`` gate would treat that damaged tree as proven
                    # complete — forwarding files the interrupted cleanup deleted (not the
                    # agent) as credential-hiding whiteouts. Clearing the marker first
                    # makes an interrupted cleanup degrade to the safe direction instead.
                    # We already captured ``legacy_complete`` above, so the reconcile's
                    # deletion-forwarding decision is unaffected by removing it now.
                    try:
                        (target_root / _CLAUDE_LEGACY_COMPLETE_MARKER).unlink(missing_ok=True)
                    except OSError as exc:
                        # Clearing the marker is the one non-best-effort step in this
                        # cleanup path, and the overlay is already prepared+reconciled
                        # above. A transient/readonly/permission failure removing it must
                        # not fail auth provisioning over a cleanup-only problem. Crucially
                        # we must NOT fall through to the ``rmtree``: a marker we could not
                        # clear, left over a tree the reap then partially removes, is the
                        # exact credential-hiding state the clear-before-reap ordering
                        # exists to prevent. Leaving both the still-complete legacy tree and
                        # its valid marker intact keeps the "marker present ⟹ complete tree"
                        # invariant true, so a later provision can retry the reap safely.
                        _log.info(
                            "claude_auth_overlay_legacy_reap_deferred",
                            reason_code=_CLAUDE_AUTH_OVERLAY_LEGACY_REAP_DEFERRED,
                            workspace_auth_root=str(target_root),
                            error=str(exc),
                        )
                    else:
                        shutil.rmtree(legacy_claude_copy, ignore_errors=True)
        else:
            target_dir = legacy_claude_copy
            target_root.mkdir(parents=True, exist_ok=True)
            if not target_dir.exists():
                # No live legacy copy exists, so any ``.claude-complete`` marker still
                # sitting beside it is stale — it can only have been left dangling by an
                # interrupted older-code reap (one that removed ``.claude`` without
                # clearing its marker first, the inverse of the clear-before-rmtree
                # ordering this module now enforces). ``staged_replace_won`` only stops
                # *this* provision from *adding* a marker; it never neutralizes a
                # pre-existing one. Clear it before racing to materialize a new copy so
                # the build starts neutral: if our atomic ``replace`` below loses to a
                # concurrent *partial* pre-atomic ``.claude`` winner we would otherwise
                # leave that stale marker vouching for the winner's incomplete tree,
                # re-enabling the destructive deletion whiteouts the completeness gate
                # exists to suppress. A winning writer re-asserts the marker after its own
                # atomic replace lands; clearing here only ever forgoes a destructive
                # pass (the safe direction), never hides a credential. Best-effort: a
                # transient unlink fault must not fail provisioning — the worst case is a
                # surviving stale marker, which the reconcile already treats conservatively
                # only when paired with a reused (never freshly built) copy.
                with contextlib.suppress(OSError):
                    (target_root / _CLAUDE_LEGACY_COMPLETE_MARKER).unlink(missing_ok=True)

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
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError from a corrupted/binary marker:
        # treat an unreadable pin as "no recoverable base" rather than crashing
        # provisioning, so the caller rebuilds from the current host.
        return None
    base = _shared_claude_base_dir(work_dir, signature)
    return base if base.is_dir() else None


def _pin_matches_signature(sig_marker: Path, signature: str) -> bool:
    """Return whether ``base.signature`` pins exactly the current-host ``signature``.

    A surviving overlay ``upper`` is *verifiable* when either the pinned base is still
    on disk (:func:`_pinned_overlay_base`) or — even if that base dir was reaped — the
    pin names the same signature a current-host rebuild would reproduce. This probe is
    the second case: ``True`` iff the marker exists and its stripped contents equal
    ``signature``, so rebuilding the base from the current host yields exactly the lower
    the upper was built against (no guess). An absent or unreadable marker (``OSError``,
    or ``ValueError``/``UnicodeDecodeError`` from corrupted bytes) is not a match, so the
    caller treats the upper as unverifiable and discards it (#405).
    """

    try:
        return sig_marker.read_text().strip() == signature
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError from a corrupted/binary marker
        # (UnicodeDecodeError subclasses ValueError, not OSError): treat an
        # unreadable marker as "not a match" so the caller discards the
        # unverifiable upper instead of crashing provisioning (#405).
        return False


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
        # #405 owner decision: a surviving *non-empty* ``upper`` whose base cannot be
        # verified must NOT be remounted over a base guessed from the current host —
        # that would stack an upper built against an unknown lower over a different
        # base (a wrong-base correctness gap if ``~/.claude`` changed). The base is
        # unverifiable here only because ``_pinned_overlay_base`` already returned
        # ``None`` (no pin, or a pin whose base dir is gone); it is still *verifiable*
        # when the pin names the current-host signature, so a rebuild reproduces the
        # exact lower (``_pin_matches_signature`` — the ``test_overlay_retry_rebuilds_
        # when_pinned_base_missing`` KEEP case). A live overlay still mounted is also
        # verifiable: the reuse branch below recovers its real lowerdir, so an unpinned
        # upper there is never discarded (``not is_mounted`` keeps the live-reuse tests
        # green). An *empty* leftover upper carries no agent data and is a fresh start,
        # so the silent normal path handles it (``_overlay_upper_has_data`` — the
        # ``test_crash_before_mount`` provision-2 case). When all three say the upper is
        # non-empty, unverifiable, and not live, discard + rebuild from the current host
        # so ~/.claude (credentials included) is re-derived fresh — the owner ruling that
        # wrong-base-correctness wins over credential-preservation. Made LOUD so it is
        # auditable and never a silent discard.
        if (
            upper.exists()
            and _overlay_upper_has_data(upper)
            and not _pin_matches_signature(sig_marker, fresh_signature)
            # Broad guard: do not discard an overlay that is already live.
            and not overlay_mounter.is_mounted(merged)
            # Re-observe the live mount as the *last* thing before the destructive
            # ``rmtree`` below (which runs as the first body statement, so nothing
            # sits between this check and the delete). Two provisions for the same
            # workspace can race: a concurrent caller can win the mount in the window
            # after the broad guard above observed ``merged`` unmounted, mounting this
            # very ``upper``/``work`` as the live overlay's backing layers. The
            # ``rmtree`` would then yank a running overlay's upperdir/workdir and
            # destroy the active workspace's Claude edits — and the EBUSY/live-mount
            # reuse branch below cannot protect it, because the delete happens before
            # any mount attempt. If it went live in that window, skip the discard so
            # the ``is_mounted(merged)`` reuse branch below adopts the live overlay
            # instead of clobbering it.
            and not overlay_mounter.is_mounted(merged)
        ):
            # Drop the unverifiable upper/work so the dirs are recreated empty below
            # and a fresh empty upper is mounted over the current-host base.
            # ``ignore_errors`` mirrors the sibling reaps: a stuck cleanup must not
            # fail provisioning. The delete runs first — before the log — so nothing
            # widens the recheck-to-delete window above.
            shutil.rmtree(upper, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
            _log.warning(
                "claude_auth_overlay_unpinned_upper_discarded_rebuilt",
                reason_code=_CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT,
                workspace_auth_root=str(claude_root),
            )
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
