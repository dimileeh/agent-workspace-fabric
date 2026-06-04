"""GC-B: a safe reaper for superseded shared ``~/.claude`` overlay bases.

Split out of :mod:`awf.service.gc` to keep that module under the first-party
line-count guardrail (cf. :mod:`awf.service.gc_auth_overlay`,
:mod:`awf.service.gc_classify`); the documented ``awf.service.gc.<name>`` surface
is preserved by re-exporting these names from ``gc``.

Each distinct host ``~/.claude`` signature builds a new
``auth/_shared/claude-base/<signature>/.claude`` base (~1.7 GB). Terminal-workspace
GC enumerates candidates from DB rows and deliberately never enters ``_shared``, so
old signature bases accumulate forever once the operator changes ``~/.claude``
(closes #389). This reaper removes *superseded* bases while never touching one that
is still in use:

- the **current** host signature's base (it backs every new provision),
- any base that is **live-mounted** as an overlay ``lowerdir=`` in ``/proc/mounts``
  (overlayfs forbids removing a live lower), and
- any base that is **pinned** by a surviving workspace's ``base.signature`` marker
  (a torn-down overlay whose ``upper`` may yet remount against it).

In-progress ``.claude-base-*`` staging dirs are #379's domain and are left alone.
The removal is hard-guarded to paths under ``auth/_shared/claude-base`` and a
permission-denied reap is surfaced loudly as a ``partial`` step, never a silent
success. Base contents/secrets are never logged.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from awf.common.logging import get_logger
from awf.node.auth_mounts import (
    _CLAUDE_BASE_DIRNAME,
    _OVERLAY_UNMOUNTED_MARKER,
    _PROC_MOUNTS,
    _SHARED_AUTH_DIRNAME,
    _has_cap_sys_admin,
    _host_claude_signature,
    _overlay_upper_has_data,
    _shared_claude_base_dir,
    iter_overlay_lowerdirs,
)

_log = get_logger(__name__)

# A superseded base dir was reclaimed (the step's reason code when reaping ran
# without a permission failure).
CLAUDE_BASE_SUPERSEDED_REAPED = "CLAUDE_BASE_SUPERSEDED_REAPED"
# A dry-run pass identified superseded bases but removed nothing from disk. Kept
# distinct from ``CLAUDE_BASE_SUPERSEDED_REAPED`` so monitoring keyed on a reason
# code alone can tell an execute that reclaimed disk apart from a plan-only preview
# without also having to inspect ``execute`` / ``reaped``.
CLAUDE_BASE_SUPERSEDED_PLANNED = "CLAUDE_BASE_SUPERSEDED_PLANNED"
# A specific base dir could not be removed because the process lacked permission
# (e.g. a root-owned base under a uid-1000 caller). Surfaced per-error so the step
# is a loud ``partial`` rather than reporting success while disk stays leaked.
CLAUDE_BASE_REAP_PERMISSION_DENIED = "CLAUDE_BASE_REAP_PERMISSION_DENIED"
# A reap candidate resolved outside the shared base root and was refused before any
# ``rmtree`` (a structural/path-safety failure, not a filesystem permission error).
# Kept distinct from ``CLAUDE_BASE_REAP_PERMISSION_DENIED`` so alerting keyed on
# uid-1000-vs-root permission denials is not conflated with this defensive guard.
CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT = "CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT"
# At least one base could not be reaped (permission or other OSError): the whole
# step is ``partial``.
CLAUDE_BASE_REAP_PARTIAL = "CLAUDE_BASE_REAP_PARTIAL"
# Nothing to do: the base root is absent, or every base is protected (current /
# live-mounted / pinned) so no superseded base exists.
CLAUDE_BASE_GC_NOOP = "CLAUDE_BASE_GC_NOOP"
# The reaper ran in a process that cannot see the worker's mount namespace (the API
# container binds the work dir ``rprivate``; only the worker holds ``CAP_SYS_ADMIN``
# and the ``rshared`` bind) *and* a surviving overlay carries no ``base.signature``
# pin. Such a base could be a live worker-mounted lower that is invisible here and
# has no pin to fall back on, so every candidate is conservatively protected this
# pass rather than risk reaping a base still backing a running agent. Mirrors
# ``auth_mounts.OverlayUnmountUnverifiableError`` (PRRT_kwDOSJAM6s6HI1tS).
CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE = "CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE"

# Marker file each overlay-backed workspace writes recording which shared base its
# ``upper`` belongs to (see ``auth_mounts._record_overlay_base_pin``).
_BASE_SIGNATURE_MARKER = "base.signature"


def _claude_base_root(work_dir: Path) -> Path:
    """Return the shared overlay-base root ``auth/_shared/claude-base`` under work_dir."""

    return work_dir / "auth" / _SHARED_AUTH_DIRNAME / _CLAUDE_BASE_DIRNAME


def _pinned_base_dirs(
    work_dir: Path, *, pruned_auth_dirs: frozenset[Path] = frozenset()
) -> set[Path]:
    """Return the shared base dirs pinned by surviving workspaces' ``base.signature``.

    A torn-down overlay leaves its ``upper`` and a ``base.signature`` marker on
    disk; a later provision can remount ``upper`` against exactly that base. Reaping
    a pinned base would strand the surviving ``upper`` (it could never recover the
    agent's mutations), so every pinned base is protected. The ``_shared`` tree
    itself is skipped — it holds the bases, not a workspace's auth dir.

    ``pruned_auth_dirs`` lists auth dirs that the caller will delete in this GC pass
    (the terminal candidates). On execute their pins are already gone from disk before
    the reaper runs; on a dry run nothing is deleted, so their pins are skipped here so
    the preview matches what the same candidate set frees on execute
    (PRRT_kwDOSJAM6s6HIepf).
    """

    auth_root = work_dir / "auth"
    pruned = {auth_dir.resolve(strict=False) for auth_dir in pruned_auth_dirs}
    pinned: set[Path] = set()
    try:
        workspace_dirs = list(auth_root.iterdir())
    except OSError:
        return pinned
    for workspace_dir in workspace_dirs:
        if workspace_dir.name == _SHARED_AUTH_DIRNAME:
            continue
        if workspace_dir.resolve(strict=False) in pruned:
            continue
        marker = workspace_dir / "claude" / _BASE_SIGNATURE_MARKER
        try:
            signature = marker.read_text().strip()
        except OSError:
            continue
        if not signature:
            continue
        pinned.add(_shared_claude_base_dir(work_dir, signature))
    return pinned


def _has_unverifiable_live_overlay(
    work_dir: Path, *, pruned_auth_dirs: frozenset[Path] = frozenset()
) -> bool:
    """Return whether a surviving overlay may be live but cannot be pinned to a base.

    A workspace whose ``claude/upper`` overlay scratch carries data but has
    *neither* a ``base.signature`` pin *nor* an ``.overlay-unmounted`` teardown
    marker may still hold a **live** overlay in the worker's mount namespace, mounted
    against a base that this process cannot observe via its own ``/proc/mounts`` — the
    API container binds the work dir ``rprivate`` and lacks ``CAP_SYS_ADMIN``, so a
    worker-mounted lowerdir never appears here. Such a base is invisible to the
    live-mount protection *and* has no pin to fall back on, so it cannot be safely
    classified as superseded. Mirrors the incapable branch of
    :func:`auth_mounts.teardown_workspace_auth_overlay`. An *empty* ``upper`` (a
    failed first mount that fell back to the legacy full copy) is not a live overlay
    and is ignored, matching provisioning's ``_overlay_upper_has_data`` check.
    ``pruned_auth_dirs`` (the terminal candidates this pass deletes) are excluded —
    they will not remount.
    """

    auth_root = work_dir / "auth"
    pruned = {auth_dir.resolve(strict=False) for auth_dir in pruned_auth_dirs}
    try:
        workspace_dirs = list(auth_root.iterdir())
    except OSError:
        return False
    for workspace_dir in workspace_dirs:
        if workspace_dir.name == _SHARED_AUTH_DIRNAME:
            continue
        if workspace_dir.resolve(strict=False) in pruned:
            continue
        claude_root = workspace_dir / "claude"
        if not _overlay_upper_has_data(claude_root / "upper"):
            # No overlay scratch (legacy full-copy workspace, or never provisioned),
            # or only an *empty* ``upper`` left by a failed first mount that fell back
            # to the legacy full copy: nothing that could be a live overlay lower.
            # Provisioning ignores such an empty ``upper`` via the same
            # ``_overlay_upper_has_data`` check, so an empty leftover here must not
            # masquerade as a possibly-live overlay and block GC-B indefinitely.
            continue
        if (claude_root / _OVERLAY_UNMOUNTED_MARKER).exists():
            # A capable process (the worker) recorded teardown: the old base is no
            # longer a live lower, so classifying it is safe.
            continue
        marker = claude_root / _BASE_SIGNATURE_MARKER
        try:
            signature = marker.read_text().strip()
        except OSError:
            signature = ""
        if not signature:
            return True
    return False


def _protected_signature_dirs(
    *,
    work_dir: Path,
    host_home: Path,
    base_root: Path,
    proc_mounts: Path,
    pruned_auth_dirs: frozenset[Path] = frozenset(),
) -> set[Path]:
    """Return the ``<signature>`` dirs that must never be reaped.

    The union of: the current host signature's base (when ``host_home/.claude``
    exists), every live-mounted overlay ``lowerdir`` that resolves under
    ``base_root``, and every pinned base. Protection is computed at ``<signature>``
    dir granularity because the reaper removes the whole ``<signature>`` dir.

    Computed in full *before* any removal so a base that becomes protected mid-scan
    is never reaped. An unreadable ``host_home`` simply omits the current-signature
    protection; live-mounted and pinned bases are still protected unconditionally.
    ``pruned_auth_dirs`` are excluded from pin computation (see ``_pinned_base_dirs``).
    """

    protected_bases: set[Path] = set()
    if (host_home / ".claude").exists():
        protected_bases.add(_shared_claude_base_dir(work_dir, _host_claude_signature(host_home)))
    protected_bases.update(iter_overlay_lowerdirs(proc_mounts))
    protected_bases.update(_pinned_base_dirs(work_dir, pruned_auth_dirs=pruned_auth_dirs))

    protected_dirs: set[Path] = set()
    for base in protected_bases:
        # A live ``lowerdir`` may point anywhere (other overlays on the host); keep
        # only those that resolve to a ``<signature>/.claude`` base under our root.
        # Compare in *resolved* form: live lowerdirs read from ``/proc/mounts`` are
        # kernel-resolved (the kernel follows symlinks when recording mount paths),
        # while host/pinned bases are built from a ``work_dir`` that only had
        # ``expanduser()`` applied. If ``work_dir`` is reached via a symlink (e.g. a
        # bind-mount alias) the two string forms diverge; without resolving, a live
        # base would silently drop out of the protected set and the reaper would
        # ``rmtree`` a live lower — overlayfs refuses with ``EBUSY``, so no data is
        # lost, but every execute pass would report a spurious ``partial``. ``base_root``
        # is already resolved by the caller, so resolving each candidate keeps the
        # comparison (and the membership test in the scan loop) consistent.
        signature_dir = base.parent
        if signature_dir.parent.resolve(strict=False) == base_root:
            protected_dirs.add(signature_dir.resolve(strict=False))
    return protected_dirs


def reap_superseded_claude_bases(
    *,
    work_dir: Path,
    host_home: Path,
    proc_mounts: Path = _PROC_MOUNTS,
    execute: bool = False,
    pruned_auth_dirs: frozenset[Path] = frozenset(),
    capability_probe: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Reap superseded shared ``~/.claude`` overlay bases under ``work_dir`` (#389).

    Scans ``auth/_shared/claude-base/<signature>`` dirs and removes those that hold
    a completed base (a ``.claude`` child) and are **not** protected — i.e. not the
    current host signature, not live-mounted, not pinned. ``execute=False`` (the
    default) plans without deleting. ``pruned_auth_dirs`` lists auth dirs the caller
    deletes in this GC pass; their ``base.signature`` pins are ignored so a dry-run
    preview matches what the same candidate set frees on execute. Returns an
    inspectable report:

    ``status`` (``ok`` / ``partial`` / ``skipped``), ``execute``, ``base_root``,
    ``scanned`` / ``protected`` / ``reaped`` / ``planned`` / ``unverifiable``
    signature lists, and per-signature ``errors``. A permission-denied removal makes
    ``status`` ``partial`` (reason ``CLAUDE_BASE_REAP_PARTIAL``); the per-error reason
    distinguishes a permission denial (``CLAUDE_BASE_REAP_PERMISSION_DENIED``) from
    other ``OSError``.

    ``capability_probe`` (defaulting to :func:`auth_mounts._has_cap_sys_admin`)
    reports whether this process can observe the worker's overlay mounts. When it
    cannot (the API container) *and* a surviving overlay has no ``base.signature``
    pin, the ``/proc/mounts`` live-mount protection is untrustworthy and a live base
    could be invisible *and* unpinned — so every candidate is conservatively
    protected (listed under ``unverifiable``) and ``reason_code`` becomes
    ``CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE`` rather than reaping a base that may
    still back a running agent (PRRT_kwDOSJAM6s6HI1tS).
    """

    work_dir = Path(work_dir).expanduser()
    host_home = Path(host_home).expanduser()
    # Resolve the base root to its canonical form so it matches the kernel-resolved
    # lowerdir paths read from ``/proc/mounts`` in ``_protected_signature_dirs`` even
    # when ``work_dir`` is reached via a symlink. ``iterdir()`` below then yields
    # canonical ``<signature>`` dirs and ``_reap_one_base``'s parent guard stays exact.
    base_root = _claude_base_root(work_dir).resolve(strict=False)

    report: dict[str, object] = {
        "status": "skipped",
        "reason_code": CLAUDE_BASE_GC_NOOP,
        "execute": execute,
        "base_root": str(base_root),
        "scanned": [],
        "protected": [],
        "reaped": [],
        "planned": [],
        "unverifiable": [],
        "errors": [],
    }
    try:
        signature_dirs = sorted(p for p in base_root.iterdir() if p.is_dir())
    except OSError:
        # No base root yet (overlay never used here), or it is unreadable. Nothing
        # to reap — a clean no-op, not a failure.
        return report

    protected_dirs = _protected_signature_dirs(
        work_dir=work_dir,
        host_home=host_home,
        base_root=base_root,
        proc_mounts=proc_mounts,
        pruned_auth_dirs=pruned_auth_dirs,
    )

    # If this process cannot see the worker's mount namespace (the API container),
    # the ``/proc/mounts`` live-mount protection above is blind to worker-mounted
    # overlays. When a surviving overlay also has no ``base.signature`` pin, neither
    # protection can vouch for its base, so reaping any candidate risks removing a
    # live lower. Decline to reap this pass instead (PRRT_kwDOSJAM6s6HI1tS).
    probe = capability_probe or _has_cap_sys_admin
    live_mount_unverifiable = not probe() and _has_unverifiable_live_overlay(
        work_dir, pruned_auth_dirs=pruned_auth_dirs
    )

    scanned: list[str] = []
    protected_names: list[str] = []
    reaped: list[str] = []
    planned: list[str] = []
    unverifiable: list[str] = []
    errors: list[dict[str, str]] = []

    for signature_dir in signature_dirs:
        # Staging dirs (``.claude-base-*``) are created inside ``<sig>/`` dirs (see
        # ``_reap_stale_claude_base_staging``'s ``*/.claude-base-*`` glob), never as
        # direct children of base_root, so they are invisible to the shallow
        # ``iterdir()`` above — #379's staging reaper owns their lifecycle. Skip any
        # ``<sig>/`` dir that has no completed ``.claude`` base yet: a mid-build
        # partial tree, or a legacy dir with unrelated content — not a superseded
        # base to reclaim.
        if not (signature_dir / ".claude").is_dir():
            continue
        scanned.append(signature_dir.name)
        if signature_dir in protected_dirs:
            protected_names.append(signature_dir.name)
            continue
        if live_mount_unverifiable:
            # Cannot verify live worker mounts here and a surviving overlay has no
            # pin: this base might back a running agent. Protect it conservatively
            # rather than reap a base that could be an invisible live lower.
            protected_names.append(signature_dir.name)
            unverifiable.append(signature_dir.name)
            continue
        if not execute:
            planned.append(signature_dir.name)
            continue
        error = _reap_one_base(signature_dir, base_root=base_root)
        if error is None:
            reaped.append(signature_dir.name)
        else:
            errors.append({"signature": signature_dir.name, **error})

    report["scanned"] = scanned
    report["protected"] = protected_names
    report["reaped"] = reaped
    report["planned"] = planned
    report["unverifiable"] = unverifiable
    report["errors"] = errors
    report["status"], report["reason_code"] = _reap_status(
        reaped=reaped, planned=planned, errors=errors
    )
    if unverifiable:
        # A superseded base was identified but deliberately not reaped because the
        # live-mount view is untrustworthy and the overlay is unpinned. Surface it
        # loudly with a dedicated reason code so this is never a silent no-op.
        report["reason_code"] = CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE
        _log.warning(
            "gc.claude_base_reap_live_mount_unverifiable",
            reason_code=CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE,
            base_root=str(base_root),
            unverifiable=unverifiable,
        )
    return report


def _reap_status(
    *,
    reaped: list[str],
    planned: list[str],
    errors: list[dict[str, str]],
) -> tuple[str, str]:
    """Return the ``(status, reason_code)`` summarizing a reap pass."""

    if errors:
        return "partial", CLAUDE_BASE_REAP_PARTIAL
    if reaped:
        return "ok", CLAUDE_BASE_SUPERSEDED_REAPED
    if planned:
        # Dry-run with superseded bases identified: a successful plan, not a no-op.
        # Distinct reason code so a plan-only preview is never mistaken for an
        # execute that actually reclaimed disk.
        return "ok", CLAUDE_BASE_SUPERSEDED_PLANNED
    return "skipped", CLAUDE_BASE_GC_NOOP


def _reap_one_base(signature_dir: Path, *, base_root: Path) -> dict[str, str] | None:
    """Remove one superseded ``<signature>`` dir; return an error dict or ``None``.

    Hard-guards that ``signature_dir`` is a direct child of ``base_root`` before
    ``rmtree`` so a malformed path can never escape the shared-base root. A
    permission-denied removal is classified loudly (so the step becomes ``partial``)
    and distinguished from other ``OSError``. Base contents/secrets are never logged.
    """

    if signature_dir.parent != base_root:
        # Defensive: every candidate comes from ``base_root.iterdir()`` so this
        # cannot trigger in practice, but refuse to ``rmtree`` anything outside the
        # base root rather than trust the caller.
        return {
            "reason_code": CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT,
            "error": "refused to reap a base outside the shared base root",
        }
    try:
        shutil.rmtree(signature_dir)
    except FileNotFoundError:
        # The base was deleted concurrently (another GC pass or an operator) between
        # the scan and this ``rmtree``. The desired end-state — the superseded base is
        # gone — already holds, so treat it as a success rather than a partial failure
        # that would raise a false-positive alert.
        return None
    except PermissionError as exc:
        # The most common real failure: a root-owned base under a uid-1000 caller.
        # Python maps ``EACCES``/``EPERM`` to ``PermissionError``, so this catches the
        # permission denials; classify it loudly and distinctly for alerting.
        _log.warning(
            "gc.claude_base_reap_permission_denied",
            reason_code=CLAUDE_BASE_REAP_PERMISSION_DENIED,
            signature_dir=str(signature_dir),
            error=str(exc),
        )
        return {"reason_code": CLAUDE_BASE_REAP_PERMISSION_DENIED, "error": str(exc)}
    except OSError as exc:
        # A non-permission OSError (EBUSY/EROFS/ENOTEMPTY/…): still a partial-making
        # failure, but not a permission denial — keep the distinction so alerting
        # keyed on the two reason codes does not conflate them.
        _log.warning(
            "gc.claude_base_reap_failed",
            reason_code=CLAUDE_BASE_REAP_PARTIAL,
            signature_dir=str(signature_dir),
            error=str(exc),
        )
        return {"reason_code": CLAUDE_BASE_REAP_PARTIAL, "error": str(exc)}
    return None
