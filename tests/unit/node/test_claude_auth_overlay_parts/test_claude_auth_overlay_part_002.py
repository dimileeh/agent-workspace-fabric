"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 2).

Continuation of :mod:`test_claude_auth_overlay_part_001`; the shared
:class:`FakeOverlayMounter` and ``_seed_host_claude`` helper are imported from
that part so every test in this package exercises the same fakes.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node import auth_mounts as auth_mounts_mod
from awf.node.auth_mounts import (
    _CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE,
    _OVERLAY_UNMOUNTED_MARKER,
    OverlayUnmountUnverifiableError,
    _has_cap_sys_admin,
    _host_claude_signature,
    _overlay_filesystem_available,
    _shared_claude_base_dir,
    _SubprocessOverlayMounter,
    claude_auth_isolation_label,
    default_overlay_mounter,
    resolve_service_auth_mounts,
    teardown_workspace_auth_overlay,
)

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter as FakeOverlayMounter,
)
from .test_claude_auth_overlay_part_001 import (
    _seed_host_claude as _seed_host_claude,
)


@pytest.mark.unit
def test_prepin_upper_mount_failure_does_not_pin_guessed_base(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # A pre-pin overlay left by an older build: ``upper``/``work`` survive but no
    # ``base.signature`` marker was ever recorded, so the original base the
    # surviving ``upper`` was built against is unknowable.
    claude_root = work_dir / "auth" / "ws_prepin" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)
    surviving = claude_root / "upper" / "settings.json"
    surviving.write_text('{"theme": "agent-edited"}\n')

    # The mount fails (the surviving upper does not line up with the freshly
    # hashed host base) and ``merged`` never goes live.
    mounter = FakeOverlayMounter(supported=True, mount_error=OSError("upper/base mismatch"))

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_prepin",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    # Degraded to the legacy full copy; the surviving upper is preserved for a
    # future retry to recover.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert surviving.read_text() == '{"theme": "agent-edited"}\n'
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)
    # The signature marker must NOT be left pinning a base the mount never
    # validated against. The recorded signature was only a guess from the current
    # host hash; persisting it would lock every later retry to the wrong lowerdir
    # so the surviving upper could never remount (even after the host reverts).
    assert not (claude_root / "base.signature").exists()


@pytest.mark.unit
def test_crash_before_mount_does_not_pin_stale_base_for_fresh_provision(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    class KilledMidMountMounter(FakeOverlayMounter):
        """Models the worker being hard-killed (SIGKILL/reboot) during ``mount``.

        A signal-driven kill is not an ``OSError``/``SubprocessError``, so it is
        *not* caught by the mount fallback handler and no cleanup runs — exactly
        the window a graceful-failure ``unlink`` could never cover.
        """

        def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
            raise KeyboardInterrupt("worker killed mid-mount")

    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    # Provision 1: a *fresh* provision (no prior upper, no legacy copy) builds the
    # base and creates the empty scratch dirs, then the worker is killed before the
    # mount establishes the overlay. The empty ``upper`` survives on disk.
    with pytest.raises(KeyboardInterrupt):
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_killed",
            host_env={},
            overlay_mounter=KilledMidMountMounter(supported=True),
        )
    claude_root = work_dir / "auth" / "ws_killed" / "claude"
    assert (claude_root / "upper").is_dir()
    assert not any((claude_root / "upper").iterdir())
    # The crux of the fix: no pin marker may survive, because the overlay never
    # actually mounted. (Before the fix the marker was written ahead of the mount
    # and would persist through the kill, pinning a base no overlay-backed
    # workspace ever ran against.)
    assert not (claude_root / "base.signature").exists()

    # The operator updates ``~/.claude`` before the retry, so a fresh provision
    # must reflect the *new* host content, not the base captured pre-kill.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a

    # Provision 2 (retry): with no surviving marker the empty ``upper`` is treated
    # as a fresh start — the base is recomputed from the current host and mounted,
    # never the stale pre-kill base.
    mounter = FakeOverlayMounter(supported=True)
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_killed",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert mounter.mounts[-1]["lowerdir"] == base_b
    assert mounter.mounts[-1]["lowerdir"] != base_a
    # The marker is now recorded post-mount, pinning the base the overlay truly ran
    # against so a later teardown+retry remounts correctly.
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)


@pytest.mark.unit
def test_prepin_upper_recovers_after_host_reverts_when_mount_failed(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    class BaseMismatchMounter(FakeOverlayMounter):
        """Models overlayfs rejecting an upper built against a different lower.

        The mount only succeeds when ``lowerdir`` is the base the surviving
        ``upper`` was actually built against; any other lower (a base recomputed
        from a since-changed host) fails as a real kernel mismatch would.
        """

        def __init__(self, *, allowed_base: Path) -> None:
            super().__init__(supported=True)
            self._allowed_base = allowed_base

        def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
            if Path(lowerdir) != self._allowed_base:
                raise OSError("overlay upper built against a different lower")
            super().mount(lowerdir=lowerdir, upperdir=upperdir, workdir=workdir, merged=merged)

    # Capture host state A exactly (the signature keys off ``st_mtime_ns``, so a
    # later revert must restore the mtime too, not just the content).
    settings = host_home / ".claude" / "settings.json"
    state_a_mtime_ns = settings.stat().st_mtime_ns

    # Provision 1: overlay succeeds against the host-content-A base; the agent
    # mutates the writable ``upper``.
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    mounter = BaseMismatchMounter(allowed_base=base_a)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_revert",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_revert" / "claude"
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Simulate a pre-pin overlay: the marker is absent (older build) so the base
    # is unknowable on the next provision.
    (claude_root / "base.signature").unlink()

    # Provision 2: the host changed (content B) and the overlay is torn down. The
    # base recomputed from the changed host does not match the surviving upper, so
    # the remount fails and we degrade to the legacy copy.
    mounter.mounted.clear()
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_revert",
        host_env={},
        overlay_mounter=mounter,
    )
    # The failed mount must not have pinned the guessed (host-B) base.
    assert not (claude_root / "base.signature").exists()

    # Provision 3: the operator reverts ``~/.claude`` back to content A. Because
    # the failed mount did not poison the marker, the base recomputed from the
    # reverted host matches the surviving upper again and the overlay remounts,
    # recovering the agent's mutations.
    mounter.mounted.clear()
    settings.write_text('{"theme": "dark"}\n')
    os.utime(settings, ns=(state_a_mtime_ns, state_a_mtime_ns))
    assert _shared_claude_base_dir(work_dir, _host_claude_signature(host_home)) == base_a
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_revert",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert mounter.mounts[-1]["lowerdir"] == base_a
    assert mounter.mounts[-1]["upperdir"] == claude_root / "upper"
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_teardown_unmounts_merged_before_removal(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    merged = work_dir / "auth" / "ws_t" / "claude" / "merged"
    merged.mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)
    mounter.mounted.add(merged)

    teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id="ws_t", overlay_mounter=mounter)

    assert mounter.unmounts == [merged]
    # Idempotent: a second call is a no-op once nothing is mounted.
    teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id="ws_t", overlay_mounter=mounter)
    assert mounter.unmounts == [merged]


@pytest.mark.unit
def test_teardown_noop_when_not_mounted(tmp_path: Path) -> None:
    mounter = FakeOverlayMounter(supported=True)
    teardown_workspace_auth_overlay(
        work_dir=tmp_path / "work", workspace_id="ws_none", overlay_mounter=mounter
    )
    assert mounter.unmounts == []


@pytest.mark.unit
def test_teardown_raises_without_logging_on_real_umount_error(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    merged = work_dir / "auth" / "ws_busy" / "claude" / "merged"
    merged.mkdir(parents=True)
    mounter = FakeOverlayMounter(
        supported=True, unmount_error=subprocess.CalledProcessError(32, "umount")
    )
    mounter.mounted.add(merged)

    # The failure is re-raised so callers can surface it, but it is *not* logged
    # here: each caller owns the single warning entry (with its own event name
    # and the shared reason code), so logging here too would double-record it.
    with capture_logs() as logs, pytest.raises(subprocess.CalledProcessError):
        teardown_workspace_auth_overlay(
            work_dir=work_dir, workspace_id="ws_busy", overlay_mounter=mounter
        )
    assert not any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED" for entry in logs
    )


@pytest.mark.unit
def test_teardown_with_default_mounter_is_noop_when_path_absent(tmp_path: Path) -> None:
    # No injected mounter exercises ``default_overlay_mounter`` + the real
    # ``os.path.ismount`` on a path that is not a mountpoint.
    teardown_workspace_auth_overlay(work_dir=tmp_path / "work", workspace_id="ws_absent")


@pytest.mark.unit
def test_teardown_records_marker_after_unmount(tmp_path: Path) -> None:
    # A capable unmount records the ``.overlay-unmounted`` marker so a later
    # capability-less GC can tell the overlay was released here.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_marker" / "claude"
    (claude_root / "merged").mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)
    mounter.mounted.add(claude_root / "merged")

    teardown_workspace_auth_overlay(
        work_dir=work_dir, workspace_id="ws_marker", overlay_mounter=mounter
    )

    assert mounter.unmounts == [claude_root / "merged"]
    assert (claude_root / _OVERLAY_UNMOUNTED_MARKER).is_file()


@pytest.mark.unit
def test_teardown_incapable_with_upper_and_no_marker_raises(tmp_path: Path) -> None:
    # CLI/API context (no CAP_SYS_ADMIN) cannot see the worker's mount. A
    # surviving overlay ``upper`` with no teardown marker means the worker may
    # still hold the mount, so removing the auth dir would strand it: fail loudly.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_incapable" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)  # not mounted in this namespace

    with pytest.raises(OverlayUnmountUnverifiableError) as excinfo:
        teardown_workspace_auth_overlay(
            work_dir=work_dir,
            workspace_id="ws_incapable",
            overlay_mounter=mounter,
            capability_probe=lambda: False,
        )

    assert excinfo.value.reason_code == _CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE
    assert mounter.unmounts == []


@pytest.mark.unit
def test_teardown_incapable_with_marker_is_noop(tmp_path: Path) -> None:
    # The worker already recorded a teardown marker, so a capability-less GC can
    # safely treat this as released and remove the dir.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_released" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / _OVERLAY_UNMOUNTED_MARKER).write_text("")
    mounter = FakeOverlayMounter(supported=True)

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_released",
        overlay_mounter=mounter,
        capability_probe=lambda: False,
    )

    assert mounter.unmounts == []


@pytest.mark.unit
def test_teardown_incapable_legacy_copy_without_upper_is_noop(tmp_path: Path) -> None:
    # A legacy full-copy workspace has no overlay ``upper``; there is no mount to
    # strand, so a capability-less teardown is a clean no-op.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_legacy" / "claude"
    (claude_root / ".claude").mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_legacy",
        overlay_mounter=mounter,
        capability_probe=lambda: False,
    )

    assert mounter.unmounts == []
    assert not (claude_root / _OVERLAY_UNMOUNTED_MARKER).exists()


@pytest.mark.unit
def test_teardown_capable_not_mounted_writes_marker(tmp_path: Path) -> None:
    # The worker (capable) sees the real namespace: nothing mounted means
    # teardown is verified, and it records the marker for the GC path.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_capable" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_capable",
        overlay_mounter=mounter,
        capability_probe=lambda: True,
    )

    assert mounter.unmounts == []
    assert (claude_root / _OVERLAY_UNMOUNTED_MARKER).is_file()


@pytest.mark.unit
def test_default_overlay_mounter_supported_false_under_force_copy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even with overlayfs + CAP_SYS_ADMIN both present, the force-copy request
    # (set by the bootstrap propagation preflight on non-propagating hosts) flips
    # the posture to the copy fallback so the agent never gets an empty overlay.
    monkeypatch.setattr(auth_mounts_mod, "_overlay_filesystem_available", lambda: True)
    monkeypatch.setattr(auth_mounts_mod, "_has_cap_sys_admin", lambda: True)
    assert default_overlay_mounter().supported() is True

    monkeypatch.setenv("AWF_CLAUDE_AUTH_FORCE_COPY", "1")
    assert default_overlay_mounter().supported() is False


@pytest.mark.unit
def test_teardown_capable_not_mounted_without_auth_dir_skips_marker(tmp_path: Path) -> None:
    # A capable teardown for a workspace that was never provisioned (no auth dir)
    # must not materialize an empty tree just to drop a marker.
    work_dir = tmp_path / "work"
    mounter = FakeOverlayMounter(supported=True)

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_missing",
        overlay_mounter=mounter,
        capability_probe=lambda: True,
    )

    assert mounter.unmounts == []
    assert not (work_dir / "auth" / "ws_missing").exists()


@pytest.mark.unit
def test_shared_base_build_loses_race_and_reuses_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    def _lost_race(self: Path, target: Path) -> None:
        # A concurrent provision already populated ``base``; renaming onto the
        # now non-empty directory fails. Model that winning side so the lost
        # race is distinguishable from a genuine (base-absent) failure.
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "settings.json").write_text('{"theme": "dark"}\n')
        raise OSError("base populated by a concurrent provision")

    monkeypatch.setattr(auth_mounts_mod.Path, "replace", _lost_race)

    mounter = FakeOverlayMounter(supported=True)
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race",
        host_env={},
        overlay_mounter=mounter,
    )

    # The staged build is discarded and the existing shared base path is reused.
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert mounter.mounts[0]["lowerdir"] == base
    staging_dirs = list((base.parent).glob(".claude-base-*"))
    assert staging_dirs == []


@pytest.mark.unit
def test_shared_base_build_replace_failure_logs_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    def _replace_fails(self: Path, target: Path) -> None:
        # A genuine failure (e.g. permissions) leaves ``base`` absent — this is
        # not a lost race, so it must surface log evidence rather than silently
        # returning a non-existent base.
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(auth_mounts_mod.Path, "replace", _replace_fails)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_replace_fail",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_replace_fail" / "claude"
    # The non-race replace failure degraded to the legacy full copy...
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    # ...with log evidence at both the replace site and the caller's fallback.
    assert any(entry.get("event") == "claude_auth_shared_base_replace_failed" for entry in logs)
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_SHARED_BASE_FAILED" for entry in logs)
    # No orphaned staging dir remains under the shared root.
    assert list(base.parent.glob(".claude-base-*")) == []


@pytest.mark.unit
def test_shared_base_build_copytree_failure_cleans_staging_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    real_copytree = auth_mounts_mod.shutil.copytree

    def _copytree_fails(src: object, dst: object, *args: object, **kwargs: object) -> object:
        # Only the shared-base build copies into a ``.claude-base-*`` staging
        # dir; let the legacy full-copy fallback's copytree run normally.
        if ".claude-base-" in str(dst):
            raise OSError("No space left on device")
        return real_copytree(src, dst, *args, **kwargs)

    # A copytree (or chown) failure raises before the ``replace`` block, so the
    # mkdtemp staging dir must still be reclaimed rather than orphaned under the
    # shared root, and provisioning degrades to the legacy full copy.
    monkeypatch.setattr(auth_mounts_mod.shutil, "copytree", _copytree_fails)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_copy_fail",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_copy_fail" / "claude"
    # Legacy full copy took over after the shared-base build failed.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_SHARED_BASE_FAILED" for entry in logs)
    # No orphaned staging dir remains under the shared root.
    assert list(base.parent.glob(".claude-base-*")) == []


@pytest.mark.unit
def test_shared_base_build_reaps_stale_orphan_staging_dirs(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    shared_root = base.parent
    shared_root.mkdir(parents=True)

    # A crash-orphaned staging dir from a killed provision: a full copy stranded
    # under ``_shared`` with an old mtime that GC would never reap.
    stale = shared_root / ".claude-base-orphan"
    (stale / ".claude").mkdir(parents=True)
    (stale / ".claude" / "blob").write_text("x" * 4096)
    old = time.time() - (auth_mounts_mod._STALE_STAGING_MAX_AGE_SECONDS + 60)
    os.utime(stale, (old, old))
    # A concurrent provision's in-progress staging dir (fresh mtime) must survive.
    live = shared_root / ".claude-base-live"
    live.mkdir()
    # A staging entry that stat() cannot resolve (vanished/dangling) is skipped,
    # never fatal.
    dangling = shared_root / ".claude-base-dangle"
    dangling.symlink_to(shared_root / "missing-target")

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reap",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    # The base built normally (overlay mount uses it as lowerdir) and the stale
    # orphan is gone, while the live staging dir and the unresolvable symlink are
    # left untouched.
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert base.is_dir()
    assert not stale.exists()
    assert live.exists()
    assert dangling.is_symlink()


@pytest.mark.unit
def test_shared_base_build_reaps_orphan_staging_under_superseded_signature(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # A crash-orphaned staging dir stranded under a *different* (now superseded)
    # signature: the host ``~/.claude`` changed since, so the current provision's
    # signature dir is not where this orphan lives. A per-signature sweep would
    # never revisit it; GC never enters ``_shared``. It must still be reaped.
    superseded_root = _shared_claude_base_dir(work_dir, "supersededsig0").parent
    superseded_root.mkdir(parents=True)
    stale = superseded_root / ".claude-base-orphan"
    (stale / ".claude").mkdir(parents=True)
    (stale / ".claude" / "blob").write_text("x" * 4096)
    old = time.time() - (auth_mounts_mod._STALE_STAGING_MAX_AGE_SECONDS + 60)
    os.utime(stale, (old, old))

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reap_super",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert base.is_dir()
    assert base.parent.name != "supersededsig0"
    assert not stale.exists()


@pytest.mark.unit
def test_shared_base_reuse_still_reaps_stale_orphan_staging(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # The shared base for the current signature already exists, so provisioning
    # takes the existing-base early return rather than the build path. On a host
    # whose ``~/.claude`` signature never changes, every later provision reuses
    # this base, so the staging sweep must run before (not after) that early
    # return — otherwise an orphan stranded next to a reused base is never
    # revisited (GC never enters ``_shared``).
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    shared_root = base.parent
    base.mkdir(parents=True)
    (base / "marker").write_text("prebuilt")

    # A crash-orphaned staging dir stranded next to the existing base, old enough
    # that GC would never reap it.
    stale = shared_root / ".claude-base-orphan"
    (stale / ".claude").mkdir(parents=True)
    (stale / ".claude" / "blob").write_text("x" * 4096)
    old = time.time() - (auth_mounts_mod._STALE_STAGING_MAX_AGE_SECONDS + 60)
    os.utime(stale, (old, old))

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reuse_reap",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    # The pre-existing base was reused unchanged (marker intact, still mounted
    # read-write as the overlay lowerdir), yet the stale orphan was still reaped.
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert (base / "marker").read_text() == "prebuilt"
    assert not stale.exists()


@pytest.mark.unit
def test_shared_base_is_never_under_a_workspace_auth_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    base = _shared_claude_base_dir(work_dir, "sig0")
    auth_root = work_dir / "auth"
    # The base lives under ``auth/_shared`` and never under any concrete
    # ``auth/<workspace_id>`` dir, so GC candidate enumeration cannot reap it.
    assert base.is_relative_to(auth_root / "_shared")
    for workspace_id in ("ws_a", "ws_b", "_shared0"):
        assert not base.is_relative_to(auth_root / workspace_id)


@pytest.mark.unit
def test_isolation_label_reflects_worker_overlay_capability() -> None:
    # The label reports the worker's posture from kernel overlayfs availability —
    # the one host fact shared across services — and must NOT depend on the
    # calling process's CAP_SYS_ADMIN. Otherwise the API/status path (which lacks
    # the capability the worker is granted) would misreport per_workspace_copy
    # even though the worker provisions with per_workspace_overlay.
    assert (
        claude_auth_isolation_label(overlay_filesystem_available=lambda: True)
        == "per_workspace_overlay"
    )
    assert (
        claude_auth_isolation_label(overlay_filesystem_available=lambda: False)
        == "per_workspace_copy"
    )


@pytest.mark.unit
def test_default_overlay_mounter_is_subprocess_backed() -> None:
    assert isinstance(default_overlay_mounter(), _SubprocessOverlayMounter)


@pytest.mark.unit
def test_overlay_filesystem_available_parses_proc_filesystems(tmp_path: Path) -> None:
    present = tmp_path / "with-overlay"
    present.write_text("nodev\tsysfs\nnodev\toverlay\n")
    absent = tmp_path / "without-overlay"
    absent.write_text("nodev\tsysfs\next4\n")
    assert _overlay_filesystem_available(present) is True
    assert _overlay_filesystem_available(absent) is False
    assert _overlay_filesystem_available(tmp_path / "missing") is False


@pytest.mark.unit
def test_has_cap_sys_admin_parses_proc_status(tmp_path: Path) -> None:
    granted = tmp_path / "granted"
    granted.write_text("Name:\tworker\nCapEff:\t0000003fffffffff\n")
    denied = tmp_path / "denied"
    denied.write_text("Name:\tagent\nCapEff:\t0000000000000000\n")
    no_line = tmp_path / "no-line"
    no_line.write_text("Name:\tagent\n")
    bad_hex = tmp_path / "bad-hex"
    bad_hex.write_text("CapEff:\tnot-hex\n")
    assert _has_cap_sys_admin(granted) is True
    assert _has_cap_sys_admin(denied) is False
    assert _has_cap_sys_admin(no_line) is False
    assert _has_cap_sys_admin(bad_hex) is False
    assert _has_cap_sys_admin(tmp_path / "missing") is False


@pytest.mark.unit
def test_subprocess_overlay_mounter_builds_mount_and_umount_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr(auth_mounts_mod.subprocess, "run", _run)
    mounter = _SubprocessOverlayMounter()
    mounter.mount(
        lowerdir=tmp_path / "base",
        upperdir=tmp_path / "upper",
        workdir=tmp_path / "work",
        merged=tmp_path / "merged",
    )
    mounter.unmount(tmp_path / "merged")

    assert calls[0][:4] == ["mount", "-t", "overlay", "overlay"]
    assert calls[0][4] == "-o"
    assert calls[0][5] == (
        f"lowerdir={tmp_path / 'base'},upperdir={tmp_path / 'upper'},workdir={tmp_path / 'work'}"
    )
    assert calls[0][6] == str(tmp_path / "merged")
    assert calls[1] == ["umount", str(tmp_path / "merged")]


@pytest.mark.unit
def test_subprocess_overlay_mounter_is_mounted_delegates_to_os(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(
        auth_mounts_mod.os.path,
        "ismount",
        lambda target: seen.append(target) or True,
    )
    assert _SubprocessOverlayMounter().is_mounted(tmp_path / "merged") is True
    assert seen == [tmp_path / "merged"]


@pytest.mark.unit
def test_subprocess_overlay_mounter_supported_combines_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mounts_mod, "_overlay_filesystem_available", lambda: True)
    monkeypatch.setattr(auth_mounts_mod, "_has_cap_sys_admin", lambda: True)
    assert _SubprocessOverlayMounter().supported() is True
    monkeypatch.setattr(auth_mounts_mod, "_has_cap_sys_admin", lambda: False)
    assert _SubprocessOverlayMounter().supported() is False


@pytest.mark.unit
def test_subprocess_overlay_mounter_active_lowerdir_parses_proc_mounts(
    tmp_path: Path,
) -> None:
    # Use paths containing a space so the octal-escaped form ``\040`` in
    # ``/proc/mounts`` must be decoded back to a real space.
    merged = tmp_path / "auth space" / "ws" / "claude" / "merged"
    base = tmp_path / "auth space" / "_shared" / "base" / "sig" / ".claude"
    other = tmp_path / "auth space" / "other" / "merged"
    escaped_merged = str(merged).replace(" ", "\\040")
    escaped_base = str(base).replace(" ", "\\040")
    proc_mounts = tmp_path / "mounts"
    # A non-overlay line, an overlay at a different mountpoint, then the target
    # overlay — the parser must skip the first two and read *our* lowerdir.
    proc_mounts.write_text(
        "proc /proc proc rw,nosuid 0 0\n"
        f"overlay {other} overlay rw,lowerdir={tmp_path / 'other-base'} 0 0\n"
        f"overlay {escaped_merged} overlay "
        f"rw,lowerdir={escaped_base},upperdir=/u,workdir=/w 0 0\n"
    )

    mounter = _SubprocessOverlayMounter(proc_mounts=proc_mounts)
    assert mounter.active_lowerdir(merged) == base


@pytest.mark.unit
def test_subprocess_overlay_mounter_active_lowerdir_missing_file_returns_none(
    tmp_path: Path,
) -> None:
    mounter = _SubprocessOverlayMounter(proc_mounts=tmp_path / "absent")
    assert mounter.active_lowerdir(tmp_path / "merged") is None


@pytest.mark.unit
def test_subprocess_overlay_mounter_active_lowerdir_no_lowerdir_option(
    tmp_path: Path,
) -> None:
    merged = tmp_path / "merged"
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(f"overlay {merged} overlay rw,upperdir=/u,workdir=/w 0 0\n")
    mounter = _SubprocessOverlayMounter(proc_mounts=proc_mounts)
    # An overlay line for ``merged`` that carries no ``lowerdir=`` option yields
    # ``None`` rather than a malformed guess.
    assert mounter.active_lowerdir(merged) is None


@pytest.mark.unit
def test_subprocess_overlay_mounter_active_lowerdir_unmounted_target_returns_none(
    tmp_path: Path,
) -> None:
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(f"overlay {tmp_path / 'elsewhere'} overlay rw,lowerdir=/b 0 0\n")
    mounter = _SubprocessOverlayMounter(proc_mounts=proc_mounts)
    # No overlay line matches the requested mountpoint, so there is nothing to pin.
    assert mounter.active_lowerdir(tmp_path / "merged") is None
