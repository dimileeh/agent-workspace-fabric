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
from pathlib import Path

from awf.common.logging import get_logger
from awf.node.auth_mounts import (
    _CLAUDE_BASE_DIRNAME,
    _PROC_MOUNTS,
    _SHARED_AUTH_DIRNAME,
    _host_claude_signature,
    _shared_claude_base_dir,
    iter_overlay_lowerdirs,
)

_log = get_logger(__name__)

# A superseded base dir was reclaimed (the step's reason code when reaping ran
# without a permission failure).
CLAUDE_BASE_SUPERSEDED_REAPED = "CLAUDE_BASE_SUPERSEDED_REAPED"
# A specific base dir could not be removed because the process lacked permission
# (e.g. a root-owned base under a uid-1000 caller). Surfaced per-error so the step
# is a loud ``partial`` rather than reporting success while disk stays leaked.
CLAUDE_BASE_REAP_PERMISSION_DENIED = "CLAUDE_BASE_REAP_PERMISSION_DENIED"
# At least one base could not be reaped (permission or other OSError): the whole
# step is ``partial``.
CLAUDE_BASE_REAP_PARTIAL = "CLAUDE_BASE_REAP_PARTIAL"
# Nothing to do: the base root is absent, or every base is protected (current /
# live-mounted / pinned) so no superseded base exists.
CLAUDE_BASE_GC_NOOP = "CLAUDE_BASE_GC_NOOP"

# Marker file each overlay-backed workspace writes recording which shared base its
# ``upper`` belongs to (see ``auth_mounts._record_overlay_base_pin``).
_BASE_SIGNATURE_MARKER = "base.signature"
# In-progress staging prefix owned by #379's staging reaper; never reaped here.
_STAGING_PREFIX = ".claude-base-"


def _claude_base_root(work_dir: Path) -> Path:
    """Return the shared overlay-base root ``auth/_shared/claude-base`` under work_dir."""

    return work_dir / "auth" / _SHARED_AUTH_DIRNAME / _CLAUDE_BASE_DIRNAME


def _pinned_base_dirs(work_dir: Path) -> set[Path]:
    """Return the shared base dirs pinned by surviving workspaces' ``base.signature``.

    A torn-down overlay leaves its ``upper`` and a ``base.signature`` marker on
    disk; a later provision can remount ``upper`` against exactly that base. Reaping
    a pinned base would strand the surviving ``upper`` (it could never recover the
    agent's mutations), so every pinned base is protected. The ``_shared`` tree
    itself is skipped — it holds the bases, not a workspace's auth dir.
    """

    auth_root = work_dir / "auth"
    pinned: set[Path] = set()
    try:
        workspace_dirs = list(auth_root.iterdir())
    except OSError:
        return pinned
    for workspace_dir in workspace_dirs:
        if workspace_dir.name == _SHARED_AUTH_DIRNAME:
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


def _protected_signature_dirs(
    *,
    work_dir: Path,
    host_home: Path,
    base_root: Path,
    proc_mounts: Path,
) -> set[Path]:
    """Return the ``<signature>`` dirs that must never be reaped.

    The union of: the current host signature's base (when ``host_home/.claude``
    exists), every live-mounted overlay ``lowerdir`` that resolves under
    ``base_root``, and every pinned base. Protection is computed at ``<signature>``
    dir granularity because the reaper removes the whole ``<signature>`` dir.

    Computed in full *before* any removal so a base that becomes protected mid-scan
    is never reaped. An unreadable ``host_home`` simply omits the current-signature
    protection; live-mounted and pinned bases are still protected unconditionally.
    """

    protected_bases: set[Path] = set()
    if (host_home / ".claude").exists():
        protected_bases.add(_shared_claude_base_dir(work_dir, _host_claude_signature(host_home)))
    protected_bases.update(iter_overlay_lowerdirs(proc_mounts))
    protected_bases.update(_pinned_base_dirs(work_dir))

    protected_dirs: set[Path] = set()
    for base in protected_bases:
        # A live ``lowerdir`` may point anywhere (other overlays on the host); keep
        # only those that resolve to a ``<signature>/.claude`` base under our root.
        signature_dir = base.parent
        if signature_dir.parent == base_root:
            protected_dirs.add(signature_dir)
    return protected_dirs


def reap_superseded_claude_bases(
    *,
    work_dir: Path,
    host_home: Path,
    proc_mounts: Path = _PROC_MOUNTS,
    execute: bool = False,
) -> dict[str, object]:
    """Reap superseded shared ``~/.claude`` overlay bases under ``work_dir`` (#389).

    Scans ``auth/_shared/claude-base/<signature>`` dirs and removes those that hold
    a completed base (a ``.claude`` child) and are **not** protected — i.e. not the
    current host signature, not live-mounted, not pinned. ``execute=False`` (the
    default) plans without deleting. Returns an inspectable report:

    ``status`` (``ok`` / ``partial`` / ``skipped``), ``execute``, ``base_root``,
    ``scanned`` / ``protected`` / ``reaped`` / ``planned`` signature lists, and
    per-signature ``errors``. A permission-denied removal makes ``status`` ``partial``
    (reason ``CLAUDE_BASE_REAP_PARTIAL``); the per-error reason distinguishes a
    permission denial (``CLAUDE_BASE_REAP_PERMISSION_DENIED``) from other ``OSError``.
    """

    work_dir = Path(work_dir).expanduser()
    host_home = Path(host_home).expanduser()
    base_root = _claude_base_root(work_dir)

    report: dict[str, object] = {
        "status": "skipped",
        "reason_code": CLAUDE_BASE_GC_NOOP,
        "execute": execute,
        "base_root": str(base_root),
        "scanned": [],
        "protected": [],
        "reaped": [],
        "planned": [],
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
    )

    scanned: list[str] = []
    protected_names: list[str] = []
    reaped: list[str] = []
    planned: list[str] = []
    errors: list[dict[str, str]] = []

    for signature_dir in signature_dirs:
        if signature_dir.name.startswith(_STAGING_PREFIX):
            # An in-progress / orphaned ``.claude-base-*`` staging dir: #379's
            # staging reaper owns its lifecycle (it keys off a build lock, not
            # supersession). Never touch it here.
            continue
        if not (signature_dir / ".claude").is_dir():
            # No completed base yet (a signature dir holding only mid-build staging,
            # or a partial tree). Not a superseded base to reclaim.
            continue
        scanned.append(signature_dir.name)
        if signature_dir in protected_dirs:
            protected_names.append(signature_dir.name)
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
    report["errors"] = errors
    report["status"], report["reason_code"] = _reap_status(
        reaped=reaped, planned=planned, errors=errors
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
        return "ok", CLAUDE_BASE_SUPERSEDED_REAPED
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
            "reason_code": CLAUDE_BASE_REAP_PERMISSION_DENIED,
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
