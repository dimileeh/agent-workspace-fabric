"""Auth-overlay unmount helpers for terminal-workspace GC.

Split out of :mod:`awf.service.gc` to keep that module under the first-party
line-count guardrail. These helpers cover the *unmount-before-remove* contract:
a live overlay mount at ``auth/<id>/claude/merged`` makes the subsequent
``rmtree`` of ``auth/<id>`` fail with ``EBUSY`` and strands both the mount and
the auth dir, so GC must release (or refuse to remove) the overlay first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.service.gc_results import WorkspaceGCPathOutcome

if TYPE_CHECKING:
    from awf.service.gc import WorkspaceGCCandidate
    from awf.service.gc_classify import WorkspaceGCPath

_log = get_logger(__name__)


def _unmount_candidate_auth_overlay(
    candidate: WorkspaceGCCandidate,
    *,
    work_dir: Path,
) -> tuple[str, str] | None:
    """Unmount a GC candidate's Claude auth overlay before its dir is removed.

    Unmount-before-remove: a live overlay mount at ``auth/<id>/claude/merged``
    makes the subsequent ``rmtree`` of ``auth/<id>`` fail with ``EBUSY`` and
    strands both the mount and the auth dir. The PR monitor unmounts before its
    own filesystem GC, but ``POST /v1/service/gc`` funnels through here too, so
    doing it centrally covers both entrypoints.

    Returns ``None`` when the overlay was unmounted (or there is nothing to
    release in this namespace). Returns ``(reason_code, message)`` when the
    unmount could not be performed or verified — either this process lacks
    ``CAP_SYS_ADMIN`` and cannot see the worker's mount
    (``OverlayUnmountUnverifiableError``), or ``umount`` failed outright. The
    caller skips removing the auth dir and records the failure so GC reports a
    loud ``partial`` instead of stranding a possibly-live mount while reporting
    success — consistent with running GC as root in the API container (#372).
    """

    from awf.node.auth_mounts import (
        OverlayUnmountUnverifiableError,
        teardown_workspace_auth_overlay,
    )

    try:
        teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id=candidate.workspace_id)
    except OverlayUnmountUnverifiableError as exc:
        _log.warning(
            "gc.auth_overlay_unmount_incapable",
            reason_code=exc.reason_code,
            workspace_id=candidate.workspace_id,
            error=str(exc)[:400],
        )
        return exc.reason_code, str(exc)[:400]
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "gc.auth_overlay_unmount_failed",
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED",
            workspace_id=candidate.workspace_id,
            error=repr(exc)[:400],
            # ``repr(CalledProcessError)`` drops the ``umount(8)`` stderr (e.g.
            # "target is busy"); forward it so the EBUSY root cause is greppable.
            stderr=getattr(exc, "stderr", None),
        )
        return "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED", repr(exc)[:400]
    return None


def _auth_overlay_unmount_skips_target(
    auth_unmount_failure: tuple[str, str] | None,
    target: WorkspaceGCPath,
) -> bool:
    """Return whether the auth dir delete must be skipped after an unmount failure."""

    return auth_unmount_failure is not None and target.kind == "auth"


def _auth_unmount_skipped_outcome(
    candidate: WorkspaceGCCandidate,
    target: WorkspaceGCPath,
    auth_unmount_failure: tuple[str, str] | None,
) -> WorkspaceGCPathOutcome:
    """Record the auth dir as ``skipped`` (not deleted) after an unmount failure.

    Removing it would ``rmtree`` over a mount the worker may still hold, leaking
    the overlay and its ``upper`` inodes. The matching ``delete_errors`` entry
    already drives the run to ``partial``; the skipped outcome makes the per-path
    reason visible in the GC payload.
    """

    assert auth_unmount_failure is not None  # guarded by _auth_overlay_unmount_skips_target
    reason_code, message = auth_unmount_failure
    return WorkspaceGCPathOutcome(
        workspace_id=candidate.workspace_id,
        kind=target.kind,
        path=target.path,
        status="skipped",
        reason_code=reason_code,
        error=message,
        estimated_bytes=target.estimated_bytes,
    )
