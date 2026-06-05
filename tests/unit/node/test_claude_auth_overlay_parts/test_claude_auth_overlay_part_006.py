"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 6).

Continuation of :mod:`test_claude_auth_overlay_part_001`; covers the legacy
completeness-marker reaping path when the overlay wins a reprovision — removing
the stale marker, clearing it before the legacy ``rmtree``, and deferring the
reap when the marker cannot be cleared. The ``FakeOverlayMounter``,
``_recording_mknod``, and ``_seed_overlay_reuse_with_deletion_shaped_legacy``
helpers are imported from :mod:`test_claude_auth_overlay_part_001` (split out to
keep that module under the first-party file-line guardrail).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

# Patch the modules that define the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node import auth_mounts_claude_reconcile as reconcile_mod
from awf.node import auth_mounts_overlay_copy as overlay_copy_mod
from awf.node.auth_mounts import resolve_service_auth_mounts

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter,
    _recording_mknod,
    _seed_overlay_reuse_with_deletion_shaped_legacy,
)


@pytest.mark.unit
def test_overlay_reap_removes_stale_completeness_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's #414 PRRT_kwDOSJAM6s6HRwl_ case: reaping the reconciled legacy copy
    # when the overlay wins must ALSO remove the completeness marker that lived beside it.
    # The marker's invariant is "present ⟹ *this* ``.claude`` copy is provably complete";
    # leaving it dangling after the copy is gone would let a later partial ``.claude`` (one
    # that never went through the atomic-write path — e.g. a concurrent older-code provision)
    # be falsely vouched for, so ``forward_deletions`` would stay true and the whiteout pass
    # could hide still-valid lower credentials.
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_reap_marker"
    )
    (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).touch()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod([]))

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reap_marker",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    # The legacy copy is reaped, and so is its now-dangling completeness marker.
    assert not (claude_root / ".claude").exists()
    assert not (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).exists()


@pytest.mark.unit
def test_overlay_reap_clears_marker_before_legacy_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's #414 PRRT_kwDOSJAM6s6HSG1G case: the completeness marker must be
    # invalidated *before* the (potentially multi-GB) legacy-copy ``rmtree``, not after.
    # A worker killed mid-``rmtree`` leaves a *partially removed* legacy tree; if the
    # marker is only cleared afterwards it survives, and the next provision's
    # ``legacy_complete`` gate would treat the damaged tree as proven complete — so files
    # the interrupted cleanup deleted (never the agent) become eligible for credential-
    # hiding deletion whiteouts. Clearing the marker first makes an interrupted cleanup
    # fall back to the safe direction (a missing marker forgoes the destructive pass).
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_reap_order"
    )
    (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).touch()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod([]))

    legacy_copy = claude_root / ".claude"
    marker = claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER
    real_rmtree = auth_mounts_mod.shutil.rmtree
    marker_present_at_legacy_rmtree: list[bool] = []

    def _recording_rmtree(path: object, *args: object, **kwargs: object) -> object:
        # Record the marker's state exactly when the legacy ``.claude`` reap begins: an
        # interrupted cleanup would freeze the tree in whatever state it is here, so the
        # marker must already be gone for the next provision to stay safe.
        if Path(str(path)) == legacy_copy:
            marker_present_at_legacy_rmtree.append(marker.exists())
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(auth_mounts_mod.shutil, "rmtree", _recording_rmtree)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reap_order",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    # The legacy reap ran, and the marker was already cleared before it started.
    assert marker_present_at_legacy_rmtree == [False]
    assert not legacy_copy.exists()
    assert not marker.exists()


@pytest.mark.unit
def test_overlay_reap_deferred_when_marker_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's #414 PRRT_kwDOSJAM6s6HSOLN case: clearing the completeness marker
    # before the legacy reap is the one non-best-effort step in this cleanup path. If it
    # hits a non-``FileNotFoundError`` ``OSError`` (readonly mount, EPERM, transient I/O),
    # auth provisioning must NOT fail over a cleanup-only fault — the overlay is already
    # prepared and reconciled. The reap is skipped too: a marker we could not clear, left
    # over a tree the ``rmtree`` then partially removes, is the credential-hiding state the
    # clear-before-reap ordering exists to prevent. The still-complete legacy tree and its
    # valid marker are both left intact for a later provision to retry.
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_reap_deferred"
    )
    marker = claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER
    marker.touch()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod([]))

    legacy_copy = claude_root / ".claude"
    real_unlink = Path.unlink

    def _failing_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER:
            raise OSError("read-only file system")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    real_rmtree = auth_mounts_mod.shutil.rmtree
    legacy_reaped: list[Path] = []

    def _recording_rmtree(path: object, *args: object, **kwargs: object) -> object:
        if Path(str(path)) == legacy_copy:
            legacy_reaped.append(legacy_copy)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(auth_mounts_mod.shutil, "rmtree", _recording_rmtree)

    with capture_logs() as logs:
        # A cleanup-only marker fault must not raise out of provisioning.
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_reap_deferred",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    # The reap was skipped: the still-complete legacy tree and its valid marker both
    # survive intact for a later provision to retry.
    assert legacy_reaped == []
    assert legacy_copy.exists()
    assert marker.exists()
    # The deferral is surfaced once for diagnosability.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_LEGACY_REAP_DEFERRED" for entry in logs
    )
