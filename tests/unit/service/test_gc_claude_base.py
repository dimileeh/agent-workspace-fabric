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
    CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE,
    CLAUDE_BASE_REAP_PARTIAL,
    CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT,
    CLAUDE_BASE_REAP_PERMISSION_DENIED,
    CLAUDE_BASE_SUPERSEDED_PLANNED,
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
    # A plan-only preview reports a dedicated reason code so monitoring can tell it
    # apart from an execute that actually reclaimed disk.
    assert report["reason_code"] == CLAUDE_BASE_SUPERSEDED_PLANNED
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
def test_live_base_protected_when_work_dir_reached_via_symlink(tmp_path: Path) -> None:
    # Live ``lowerdir`` paths from ``/proc/mounts`` are kernel-resolved (symlinks
    # followed), while the reaper's ``base_root`` derives from ``work_dir``. If
    # ``work_dir`` is a symlink (a bind-mount alias), an unresolved comparison would
    # drop the live base from the protected set and the reaper would try to ``rmtree``
    # a live lower — a spurious ``partial`` on every execute pass. The base root and
    # each candidate are resolved so the symlinked and kernel-resolved forms match.
    real_work = tmp_path / "real-work"
    real_work.mkdir()
    work_dir = tmp_path / "sym-work"
    work_dir.symlink_to(real_work, target_is_directory=True)
    host_home = tmp_path / "empty-home"
    host_home.mkdir()  # no ``.claude``: isolate the live-mount protection

    # Bases are created through the symlinked ``work_dir`` …
    base_live = _make_base(work_dir, "siglive00000000")
    base_super = _make_base(work_dir, "sigsuper0000000")
    # … but ``/proc/mounts`` records the kernel-resolved (real-path) lowerdir.
    resolved_live = _shared_claude_base_dir(real_work, "siglive00000000")
    assert resolved_live != base_live  # the symlinked and resolved forms differ
    proc_mounts = _proc_mounts_with_lowerdir(tmp_path, resolved_live)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        proc_mounts=proc_mounts,
        execute=True,
    )

    assert report["status"] == "ok"
    assert report["reaped"] == ["sigsuper0000000"]
    assert report["protected"] == ["siglive00000000"]
    assert report["errors"] == []
    # The live base survives; only the genuinely superseded one is reclaimed.
    assert base_live.is_dir()
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
def test_reap_one_base_refuses_symlinked_child(tmp_path: Path) -> None:
    # The hard guard must also reject a *symlinked* direct child of the base root.
    # ``Path.parent`` is purely lexical, so a symlink under the base root satisfies the
    # ``parent == base_root`` check; without an explicit ``is_symlink`` guard the only
    # thing standing between GC-B and a tree outside the base root would be
    # ``shutil.rmtree``'s incidental refusal to follow a top-level symlink. Refuse it
    # structurally instead, and prove the link target survives untouched.
    base_root = tmp_path / "claude-base"
    base_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep me")
    link = base_root / "evil"
    link.symlink_to(outside, target_is_directory=True)

    error = _reap_one_base(link, base_root=base_root)

    assert error is not None
    assert error["reason_code"] == CLAUDE_BASE_REAP_PATH_OUTSIDE_ROOT
    assert link.is_symlink()  # the link itself is left in place, not rmtree'd
    assert outside.is_dir()  # the link target tree is untouched
    assert (outside / "precious").read_text() == "keep me"


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


def _make_overlay_scratch(
    work_dir: Path,
    ws: str,
    *,
    pin: str | None = None,
    unmounted: bool = False,
    empty_upper: bool = False,
) -> Path:
    """Create a workspace overlay scratch (``claude/upper``) for ``ws``.

    By default the ``upper`` carries an entry — the writable layer of an overlay the
    agent has written through. ``empty_upper=True`` leaves it empty to model either a
    failed first mount that fell back to the legacy full copy *or* a live overlay that
    was mounted but not yet written (the pin is recorded only after a successful
    mount); the incapable-process guard cannot tell them apart, so it protects both.
    Optionally writes a ``base.signature`` pin and/or the ``.overlay-unmounted``
    teardown marker so tests can exercise the guard.
    """

    claude_root = work_dir / "auth" / ws / "claude"
    upper = claude_root / "upper"
    upper.mkdir(parents=True)
    if not empty_upper:
        (upper / "settings.json").write_text("{}\n")
    if pin is not None:
        (claude_root / "base.signature").write_text(pin + "\n")
    if unmounted:
        (claude_root / ".overlay-unmounted").write_text("")
    return claude_root


@pytest.mark.unit
def test_declines_to_reap_when_live_mount_unverifiable(tmp_path: Path) -> None:
    # The dangerous case the reaper must refuse: it runs in a process that cannot see
    # the worker's mount namespace (incapable) *and* a surviving overlay has no
    # ``base.signature`` pin, so a live worker-mounted base could be invisible here
    # and unpinned. Every candidate is conservatively protected rather than reaped.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    # A surviving overlay scratch with no pin and no teardown marker: possibly live.
    _make_overlay_scratch(work_dir, "ws_live")

    with capture_logs() as logs:
        report = reap_superseded_claude_bases(
            work_dir=work_dir,
            host_home=host_home,
            execute=True,
            capability_probe=lambda: False,
        )

    assert report["reaped"] == []
    assert report["unverifiable"] == ["sigsuper0000000"]
    assert "sigsuper0000000" in report["protected"]
    assert report["reason_code"] == CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE
    # On execute the superseded base was left on disk, so the sub-step is ``partial``
    # (not ``skipped``) — otherwise ``_gc_result`` would report a clean success and the
    # CLI would exit 0 while the multi-GB base stays leaked.
    assert report["status"] == "partial"
    assert any(
        entry.get("reason_code") == CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE for entry in logs
    )
    # The possibly-live base was not reclaimed.
    assert base_super.is_dir()


@pytest.mark.unit
def test_dry_run_unverifiable_guard_is_not_partial(tmp_path: Path) -> None:
    # A dry run reaps nothing regardless, so an unverifiable live-mount preview is an
    # honest plan, not a failed reclaim — its sub-step status stays ``skipped`` and only
    # the execute path escalates to ``partial``.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    _make_overlay_scratch(work_dir, "ws_live")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=False,
        capability_probe=lambda: False,
    )

    assert report["unverifiable"] == ["sigsuper0000000"]
    assert report["reason_code"] == CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE
    assert report["status"] == "skipped"
    assert base_super.is_dir()


@pytest.mark.unit
def test_empty_unpinned_upper_trips_unverifiable_guard(tmp_path: Path) -> None:
    # An *empty* ``claude/upper`` with no pin and no teardown marker must be treated as
    # possibly-live by an incapable process. Provisioning creates an empty ``upper``
    # before it mounts the overlay and records the ``base.signature`` pin only after a
    # successful mount, so a live overlay that was killed/in-flight before its pin write
    # (or whose agent has simply not written yet) presents exactly this state — and from
    # the capability-less API container it is indistinguishable from a failed-mount
    # legacy-fallback leftover. Reaping a base that is still a worker's invisible
    # ``lowerdir`` is irreversible data loss, so the guard must conservatively protect
    # every candidate, mirroring teardown's ``upper.exists()`` incapable branch.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    _make_overlay_scratch(work_dir, "ws_failed_mount", empty_upper=True)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: False,
    )

    assert report["reaped"] == []
    assert report["unverifiable"] == ["sigsuper0000000"]
    assert "sigsuper0000000" in report["protected"]
    assert report["reason_code"] == CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE
    # The possibly-live base was not reclaimed.
    assert base_super.is_dir()


@pytest.mark.unit
def test_capable_process_reaps_despite_unpinned_overlay_scratch(tmp_path: Path) -> None:
    # A capable process (the worker) sees the real mount namespace, so the
    # ``/proc/mounts`` live-mount protection is authoritative even for an unpinned
    # overlay — the unverifiable guard does not fire and reaping proceeds.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    _make_overlay_scratch(work_dir, "ws_live")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: True,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["unverifiable"] == []
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_unverifiable_guard_skipped_when_overlay_scratch_is_pinned(tmp_path: Path) -> None:
    # An overlay scratch that carries a ``base.signature`` pin is *not* unverifiable:
    # the pin protects its base across namespaces, so an unrelated superseded base is
    # still reaped even by an incapable process.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    base_pinned = _make_base(work_dir, "sigpinned0000000")
    _make_overlay_scratch(work_dir, "ws_live", pin="sigpinned0000000")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: False,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["unverifiable"] == []
    assert "sigpinned0000000" in report["protected"]
    assert base_pinned.is_dir()
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_unverifiable_guard_fires_when_pin_names_missing_base(tmp_path: Path) -> None:
    # A non-empty ``base.signature`` only makes an overlay verifiable when it names a
    # base that still exists on disk: ``_pinned_base_dirs`` protects exactly that
    # ``_shared_claude_base_dir(...)`` path. A worker killed mid-``write_text`` can leave
    # a truncated marker, and a marker can name a base that was already reaped or never
    # built. In those cases the pin protects a path that is *not* the real live lowerdir,
    # so the base actually backing this worker-namespace overlay is both invisible to the
    # incapable process and unprotected — exactly the state GC-B must not reap into. The
    # guard must treat it as unverifiable rather than let an unrelated superseded base be
    # reaped while the live base hides behind a stale pin.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    # Pin names a base dir that does not exist on disk (truncated/stale marker).
    _make_overlay_scratch(work_dir, "ws_live", pin="sigtrunc")

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: False,
    )

    assert report["reaped"] == []
    assert report["unverifiable"] == ["sigsuper0000000"]
    assert report["reason_code"] == CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE
    assert base_super.is_dir()


@pytest.mark.unit
def test_unverifiable_guard_skipped_when_overlay_already_unmounted(tmp_path: Path) -> None:
    # An overlay scratch with the ``.overlay-unmounted`` teardown marker was already
    # released by a capable process, so its old base is no longer a live lower and the
    # unverifiable guard does not fire — the superseded base is reaped.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    _make_overlay_scratch(work_dir, "ws_torn_down", unmounted=True)

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: False,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["unverifiable"] == []
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_unverifiable_overlay_scratch_pruned_candidate_does_not_block(tmp_path: Path) -> None:
    # An unpinned overlay scratch belonging to an auth dir the same GC pass deletes
    # (a terminal candidate) will not remount, so it must not trip the unverifiable
    # guard — the superseded base is still reaped.
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    base_super = _make_base(work_dir, "sigsuper0000000")
    claude_root = _make_overlay_scratch(work_dir, "ws_terminal")
    pruned = frozenset({claude_root.parent})

    report = reap_superseded_claude_bases(
        work_dir=work_dir,
        host_home=host_home,
        execute=True,
        capability_probe=lambda: False,
        pruned_auth_dirs=pruned,
    )

    assert report["reaped"] == ["sigsuper0000000"]
    assert report["unverifiable"] == []
    assert not base_super.parent.exists()


@pytest.mark.unit
def test_pinned_base_dirs_empty_when_auth_root_absent(tmp_path: Path) -> None:
    # ``_pinned_base_dirs`` tolerates a missing ``auth`` dir (overlay never used),
    # returning an empty set rather than raising.
    work_dir = tmp_path / "work-without-auth"
    assert _pinned_base_dirs(work_dir) == set()
