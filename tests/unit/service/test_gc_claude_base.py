"""GC-B: safe reaper for superseded shared ``~/.claude`` overlay bases (#389).

These tests build a real temp ``work_dir`` with a synthetic
``auth/_shared/claude-base/<signature>`` layout and inject a ``/proc/mounts`` file
so the live-mount protection is exercised without a real overlayfs. They assert the
reaper removes only *superseded* bases and never the current-signature,
live-mounted, or pinned base, surfaces a permission-denied removal as a loud
``partial``, and is a clean no-op when there is nothing to reclaim.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node.auth_mounts import _host_claude_signature, _shared_claude_base_dir
from awf.service import gc_claude_base as gc_claude_base_mod
from awf.service.gc_claude_base import (
    CLAUDE_BASE_GC_NOOP,
    CLAUDE_BASE_REAP_PARTIAL,
    CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT,
    CLAUDE_BASE_REAP_PERMISSION_DENIED,
    CLAUDE_BASE_SUPERSEDED_REAPED,
    _pinned_base_dirs,
    _reap_one_base,
    reap_superseded_claude_bases,
)


def _make_base(work_dir: Path, signature: str) -> Path:
    """Create a completed ``<signature>/.claude`` base under ``work_dir`` and return it."""

    base = _shared_claude_base_dir(work_dir, signature)
    base.mkdir(parents=True)
    (base / "blob").write_text("x" * 1024)
    return base


def _seed_host_claude(host_home: Path) -> str:
    """Seed a minimal host ``~/.claude`` and return its current signature."""

    claude = host_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    return _host_claude_signature(host_home)


def _proc_mounts_with_lowerdir(tmp_path: Path, base: Path) -> Path:
    """Write a ``/proc/mounts`` file whose only overlay uses ``base`` as its lower."""

    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        "proc /proc proc rw,nosuid 0 0\n"
        f"overlay {tmp_path / 'merged'} overlay rw,lowerdir={base},upperdir=/u,workdir=/w 0 0\n"
    )
    return proc_mounts


@pytest.mark.unit
def test_reaps_only_superseded_bases(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    sig_current = _seed_host_claude(host_home)

    base_a = _make_base(work_dir, sig_current)  # current signature
    base_b = _make_base(work_dir, "sigblive00000000")  # live-mounted
    base_c = _make_base(work_dir, "sigcpinned000000")  # pinned
    base_d = _make_base(work_dir, "sigdsupersede000")  # superseded → reaped

    # C is pinned by a surviving workspace's ``base.signature`` marker.
    pin_marker = work_dir / "auth" / "ws_x" / "claude" / "base.signature"
    pin_marker.parent.mkdir(parents=True)
    pin_marker.write_text("sigcpinned000000\n")

    proc_mounts = _proc_mounts_with_lowerdir(tmp_path, base_b)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        proc_mounts=proc_mounts,
        execute=True,
    )

    assert report["status"] == "ok"
    assert report["reason_code"] == CLAUDE_BASE_SUPERSEDED_REAPED
    assert report["reaped"] == ["sigdsupersede000"]
    assert set(report["protected"]) == {sig_current, "sigblive00000000", "sigcpinned000000"}
    # Only the superseded base D is gone; current / live-mounted / pinned survive.
    assert base_a.is_dir()
    assert base_b.is_dir()
    assert base_c.is_dir()
    assert not base_d.parent.exists()


@pytest.mark.unit
def test_current_signature_base_preserved_even_when_unmounted_and_unpinned(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    sig_current = _seed_host_claude(host_home)
    base_a = _make_base(work_dir, sig_current)

    # No live mounts, no pins — the current signature is still protected because it
    # backs every new provision.
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text("proc /proc proc rw 0 0\n")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        proc_mounts=proc_mounts,
        execute=True,
    )

    assert report["reaped"] == []
    assert sig_current in report["protected"]
    assert report["status"] == "skipped"
    assert report["reason_code"] == CLAUDE_BASE_GC_NOOP
    assert base_a.is_dir()


@pytest.mark.unit
def test_protects_live_and_pinned_when_host_home_has_no_claude(tmp_path: Path) -> None:
    # ``host_home`` with no ``.claude`` (operator never ran Claude on this host yet):
    # the current-signature protection is skipped, but live-mounted and pinned bases
    # are still protected unconditionally.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "empty-home"
    host_home.mkdir()

    base_live = _make_base(work_dir, "siglive00000000")
    base_super = _make_base(work_dir, "sigsuper0000000")
    proc_mounts = _proc_mounts_with_lowerdir(tmp_path, base_live)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        proc_mounts=proc_mounts,
        execute=True,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["protected"] == ["siglive00000000"]
    assert base_live.is_dir()
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_skips_signature_dir_with_only_inprogress_staging(tmp_path: Path) -> None:
    # A ``<sig>/`` dir holding only a mid-build ``.claude-base-*`` staging tree (no
    # completed ``.claude`` base yet) is #379's domain; GC-B must never touch it.
    # Staging dirs live at ``<sig>/.claude-base-*`` (see the staging reaper's
    # ``*/.claude-base-*`` glob), never as direct children of base_root, so the
    # shallow ``iterdir()`` scan yields the ``<sig>/`` dir and skips it because it has
    # no ``.claude`` child — the protection is structural, not a name check.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)

    base_root = work_dir / "auth" / "_shared" / "claude-base"
    staging = base_root / "sigbuilding00000" / ".claude-base-inprogress"
    (staging / ".claude").mkdir(parents=True)
    base_super = _make_base(work_dir, "sigsuper0000000")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert "sigsuper0000000" in report["reaped"]
    assert "sigbuilding00000" not in report["reaped"]
    assert staging.is_dir()  # the mid-build staging tree is untouched
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_skips_signature_dir_without_completed_base(tmp_path: Path) -> None:
    # A ``<signature>`` dir holding only a mid-build tree (no ``.claude``) is not a
    # superseded base to reclaim and is left alone.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)

    base_root = work_dir / "auth" / "_shared" / "claude-base"
    partial = base_root / "sigpartial000000"
    partial.mkdir(parents=True)
    (partial / "not-a-base").write_text("midbuild")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert report["scanned"] == []
    assert report["reaped"] == []
    assert partial.is_dir()


@pytest.mark.unit
def test_dry_run_plans_without_deleting(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=False,
    )

    assert report["execute"] is False
    assert report["planned"] == ["sigsuper0000000"]
    assert report["reaped"] == []
    assert report["status"] == "ok"
    assert report["reason_code"] == CLAUDE_BASE_SUPERSEDED_REAPED
    # Dry run: the superseded base is still on disk.
    assert base_super.is_dir()


@pytest.mark.unit
def test_noop_when_base_root_absent(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert report["status"] == "skipped"
    assert report["reason_code"] == CLAUDE_BASE_GC_NOOP
    assert report["scanned"] == []
    assert report["errors"] == []


@pytest.mark.unit
def test_permission_denied_reap_is_loud_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")

    def _denied(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError("Operation not permitted")

    # Patch the dependency (``shutil.rmtree``), not the reaper under test.
    monkeypatch.setattr(gc_claude_base_mod.shutil, "rmtree", _denied)

    with capture_logs() as logs:
        report = reap_superseded_claude_bases(
            work_dir=work_dir,
            host_home=host_home,
            execute=True,
        )

    assert report["status"] == "partial"
    assert report["reason_code"] == CLAUDE_BASE_REAP_PARTIAL
    assert report["reaped"] == []
    assert report["errors"] == [
        {
            "signature": "sigsuper0000000",
            "reason_code": CLAUDE_BASE_REAP_PERMISSION_DENIED,
            "error": "Operation not permitted",
        }
    ]
    assert any(entry.get("reason_code") == CLAUDE_BASE_REAP_PERMISSION_DENIED for entry in logs)
    # The base was not reclaimed (the failure was loud, not a silent success).
    assert base_super.is_dir()


@pytest.mark.unit
def test_non_permission_oserror_reap_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-permission OSError (e.g. EBUSY) also makes the step partial, but with the
    # generic reason code rather than the permission-denied one.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    _make_base(work_dir, "sigsuper0000000")

    def _busy(path: object, *args: object, **kwargs: object) -> None:
        raise OSError("device or resource busy")

    monkeypatch.setattr(gc_claude_base_mod.shutil, "rmtree", _busy)

    with capture_logs() as logs:
        report = reap_superseded_claude_bases(
            work_dir=work_dir,
            host_home=host_home,
            execute=True,
        )

    assert report["status"] == "partial"
    assert report["errors"][0]["reason_code"] == CLAUDE_BASE_REAP_PARTIAL
    assert any(entry.get("event") == "gc.claude_base_reap_failed" for entry in logs)


@pytest.mark.unit
def test_concurrent_deletion_reap_is_success_not_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A base deleted concurrently (another GC pass or an operator) between the scan and
    # the ``rmtree`` surfaces as ``FileNotFoundError`` — a subclass of ``OSError``. The
    # desired end-state (the superseded base is gone) already holds, so it must count as
    # a reaped success, not a partial failure that would raise a false-positive alert.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    _make_base(work_dir, "sigsuper0000000")

    def _vanished(path: object, *args: object, **kwargs: object) -> None:
        raise FileNotFoundError("No such file or directory")

    monkeypatch.setattr(gc_claude_base_mod.shutil, "rmtree", _vanished)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert report["status"] == "ok"
    assert report["reason_code"] == CLAUDE_BASE_SUPERSEDED_REAPED
    assert report["reaped"] == ["sigsuper0000000"]
    assert report["errors"] == []


@pytest.mark.unit
def test_pinned_marker_unreadable_or_empty_is_ignored(tmp_path: Path) -> None:
    # A workspace whose ``base.signature`` is empty (or missing) contributes no pin,
    # so an otherwise-superseded base is still reaped. The ``_shared`` tree is never
    # treated as a pinning workspace.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")

    empty_marker = work_dir / "auth" / "ws_empty" / "claude" / "base.signature"
    empty_marker.parent.mkdir(parents=True)
    empty_marker.write_text("   \n")  # whitespace-only → no pin
    # A workspace dir with no claude/base.signature at all is also ignored.
    (work_dir / "auth" / "ws_nomarker").mkdir(parents=True)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_live_lowerdir_outside_base_root_does_not_protect(tmp_path: Path) -> None:
    # A live overlay whose lowerdir points somewhere other than our base root (e.g. a
    # different overlay on the host) must not be mistaken for a protected base.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")

    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {tmp_path / 'other'} overlay rw,lowerdir={tmp_path / 'elsewhere'} 0 0\n"
    )

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        proc_mounts=proc_mounts,
        execute=True,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["protected"] == []
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_reap_one_base_refuses_path_outside_base_root(tmp_path: Path) -> None:
    # The hard guard: ``_reap_one_base`` refuses to ``rmtree`` a dir that is not a
    # direct child of the base root, even if a caller hands it one.
    base_root = tmp_path / "claude-base"
    base_root.mkdir()
    stray = tmp_path / "elsewhere" / "victim"
    stray.mkdir(parents=True)

    error = _reap_one_base(stray, base_root=base_root)

    assert error is not None
    # A dedicated structural reason code, distinct from a real filesystem permission
    # error, so alerting keyed on the two never conflates them.
    assert error["reason_code"] == CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT
    assert stray.is_dir()  # nothing was removed


@pytest.mark.unit
def test_pinned_dir_scan_tolerates_missing_auth_root(tmp_path: Path) -> None:
    # ``work_dir`` with no ``auth`` dir at all (overlay never used): the pin scan is a
    # clean empty set and the whole reaper is a no-op, never an error.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    # Use a real ``shutil`` to confirm there is genuinely nothing to do.
    assert shutil.rmtree  # sanity: module import intact

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
    )

    assert report["status"] == "skipped"
    assert report["scanned"] == []


@pytest.mark.unit
def test_pinned_base_dirs_empty_when_auth_root_absent(tmp_path: Path) -> None:
    # ``_pinned_base_dirs`` tolerates a missing ``auth`` dir (overlay never used),
    # returning an empty set rather than raising.
    work_dir = tmp_path / "work-without-auth"
    assert _pinned_base_dirs(work_dir) == set()
