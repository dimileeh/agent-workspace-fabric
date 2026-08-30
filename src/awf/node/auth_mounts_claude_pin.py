"""Overlay base-signature pin and per-workspace provision lock for Claude auth.

Split out of :mod:`awf.node.auth_mounts_claude` to keep that module under the
first-party line limit. This module holds the two mechanisms that decide *which*
shared base a surviving overlay ``upper`` belongs to and *who* may mutate a
workspace's overlay scratch at a time: the ``base.signature`` pin
(read/verify/record, plus live-mount recovery) and the ``.overlay.lock``
advisory ``flock`` that serializes the unpinned-upper discard against a
concurrent same-workspace provision's mount. :mod:`awf.node.auth_mounts_claude`
re-exports every name here, and :mod:`awf.node.auth_mounts` re-exports it in
turn, so ``awf.node.auth_mounts.<name>`` stays the stable import surface for
callers and tests.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.common.logging import get_logger
from awf.node.auth_mounts_claude_base import _shared_claude_base_dir

if TYPE_CHECKING:
    # Type-only: importing the Protocol at runtime would close an import cycle
    # (``auth_mounts_claude`` imports this module).
    from awf.node.auth_mounts_claude import OverlayMounter

_log = get_logger(__name__)

_CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED = "CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED"
# Per-workspace advisory ``flock`` marker that serializes the unpinned-upper
# discard (#405) against a concurrent same-workspace provision's overlay mount
# (#418/#419). The #405 discard re-checks ``is_mounted(merged)`` and then
# ``rmtree``s ``upper``/``work`` — a TOCTOU window in which a racing provision can
# mount the overlay over those very layers, so the discard then deletes the live
# overlay's backing dirs and destroys the active workspace's Claude edits. The
# DB-CAS provisioning claim already bars concurrent same-workspace provisions
# *except* in the stale-lease-recovery window; this exclusive ``flock`` is the
# same-host backstop for exactly that window, taken across *both* the discard and
# the mount so the ``is_mounted``-recheck + ``rmtree`` is atomic against any other
# provision's ``mount()`` (which must also hold this lock). It lives beside
# ``upper``/``work``/``merged`` and is left intact by the discard's targeted
# ``rmtree`` (only the scratch dirs are removed); GC/teardown reap ``target_root``
# wholesale and take it with them. The kernel frees the lock on ``close``/process
# death, mirroring the shared-base build lock's crash semantics.
_OVERLAY_PROVISION_LOCK_NAME = ".overlay.lock"
# Errnos that mean "the lock is held by someone else" for a non-blocking ``flock``.
# ``EAGAIN``/``EWOULDBLOCK`` normally surface as ``BlockingIOError``, but some
# systems/filesystems report the conflict as ``EACCES`` (Python's ``fcntl`` docs call
# out both forms), which arrives as a plain ``OSError``. All three are genuine
# contention, not an unsupported-locking signal.
_FLOCK_WOULD_BLOCK_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})
# Logged once when the per-workspace overlay lock file could not be created/locked
# (an FS fault: ENOSPC, EROFS, EPERM). Provisioning proceeds best-effort without
# serialization; the discard-vs-mount race reopens only under the double fault of
# (lock-file-uncreatable AND a concurrent provision), which the DB-CAS claim still
# guards against — so behavior is no worse than before the lock and is audited here.
_CLAUDE_AUTH_OVERLAY_PROVISION_LOCK_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_PROVISION_LOCK_UNAVAILABLE"


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


_OverlayLockState = Literal["acquired", "contended", "unavailable"]


@contextlib.contextmanager
def _overlay_provision_lock(claude_root: Path) -> Iterator[_OverlayLockState]:
    """Hold the per-workspace overlay ``flock`` for the discard + mount span.

    Yields one of:

    - ``"acquired"`` — this provision exclusively holds ``<claude_root>/.overlay.
      lock`` for the duration of the ``with`` block, so its unpinned-upper discard
      (``is_mounted``-recheck + ``rmtree``) is atomic against any other
      same-workspace provision's overlay ``mount()`` (which must also take this lock).
    - ``"contended"`` — a concurrent same-workspace provision already holds the lock
      (``EWOULDBLOCK``/``EAGAIN`` — or ``EACCES`` on systems that report a
      non-blocking ``flock`` conflict that way — from a non-blocking ``flock``).
      This is the narrow
      stale-lease-recovery window the DB-CAS claim does not cover; the caller must
      neither ``rmtree`` the (in-use) upper nor issue a fresh off-lock mount.
    - ``"unavailable"`` — the lock file could not be created (an FS fault: ENOSPC,
      EROFS, EPERM) **or** the FS does not support advisory locking, so ``flock``
      itself failed (ENOTSUP, ENOSYS, EINVAL). The caller proceeds best-effort
      without serialization rather than degrading to the legacy copy.

    Acquisition is strictly non-blocking (``LOCK_NB``) so it never wedges the worker;
    the kernel releases the lock on ``close``/process death, matching the shared-base
    build lock's crash semantics (:func:`_ensure_shared_claude_base`). ``flock``
    contention surfaces as ``BlockingIOError`` — or, on systems that report it as
    ``EACCES``, an ``OSError`` whose ``errno`` is in
    :data:`_FLOCK_WOULD_BLOCK_ERRNOS` — both mapped to ``"contended"``; any other
    ``OSError`` means locking is unsupported (``"unavailable"``). Only
    ``OSError``/``BlockingIOError`` are caught — never a bare ``Exception``
    (AGENTS.md rule).
    """

    lock_path = claude_root / _OVERLAY_PROVISION_LOCK_NAME
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError as exc:
        # The FS could not host the lock file (ENOSPC, EROFS, EPERM). There is no fd
        # to release, so yield ``unavailable`` directly — the caller logs once and
        # proceeds unserialized.
        _log.warning(
            "claude_auth_overlay_provision_lock_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_PROVISION_LOCK_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        yield "unavailable"
        return
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # ``EWOULDBLOCK``/``EAGAIN``: a concurrent same-workspace provision holds
            # the lock — the stale-lease window. Only genuine contention is
            # ``"contended"``; that path may degrade to the legacy copy this round.
            yield "contended"
            return
        except OSError as exc:
            if exc.errno in _FLOCK_WOULD_BLOCK_ERRNOS:
                # Some systems/filesystems report a non-blocking ``flock`` conflict as
                # ``EACCES`` rather than ``EAGAIN``/``EWOULDBLOCK`` (Python's ``fcntl``
                # docs call out both forms), so it surfaces here as a plain ``OSError``
                # instead of ``BlockingIOError``. This is still genuine contention — a
                # concurrent same-workspace provision holds the lock — so treat it like
                # the ``BlockingIOError`` path above and yield ``"contended"`` rather
                # than reopening the stale-lease discard-vs-mount race by proceeding
                # unserialized.
                yield "contended"
                return
            # The FS does not support advisory locking (``ENOTSUP``/``ENOSYS``/
            # ``EINVAL``). Treat the lock as unavailable and proceed best-effort
            # (unserialized) rather than degrading every provision to the legacy copy
            # — overlays would otherwise be permanently disabled on such filesystems.
            _log.warning(
                "claude_auth_overlay_provision_lock_unavailable",
                reason_code=_CLAUDE_AUTH_OVERLAY_PROVISION_LOCK_UNAVAILABLE,
                workspace_auth_root=str(claude_root),
                error=str(exc),
            )
            yield "unavailable"
            return
        yield "acquired"
    finally:
        # Release explicitly when held (a no-op under ``contended``, suppressed if the
        # fd's lock state is already gone), then close — the kernel also frees the
        # lock on this ``close`` and on process death.
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        # Suppress ``OSError`` on close too (e.g. ``EIO`` on a flush-on-close over
        # NFS): a raise from this ``finally`` would mask any exception already
        # propagating from the ``with`` body. The kernel frees the lock and the fd on
        # process death regardless.
        with contextlib.suppress(OSError):
            os.close(lock_fd)
