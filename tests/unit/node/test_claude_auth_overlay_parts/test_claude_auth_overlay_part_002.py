"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 2).

Continuation of :mod:`test_claude_auth_overlay_part_001`; the shared
:class:`FakeOverlayMounter` and ``_seed_host_claude`` helper are imported from
that part so every test in this package exercises the same fakes.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# Patch the module that defines the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node.auth_mounts import (
    _CLAUDE_AUTH_OVERLAY_MARKER_WRITE_FAILED,
    _CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE,
    _CLAUDE_BASE_BUILD_LOCK_NAME,
    _OVERLAY_UNMOUNTED_MARKER,
    OverlayUnmountUnverifiableError,
    _claude_base_staging_build_is_live,
    _has_cap_sys_admin,
    _host_claude_signature,
    _overlay_filesystem_available,
    _reap_stale_claude_base_staging,
    _reconcile_fallback_edits_into_upper,
    _shared_claude_base_dir,
    _SubprocessOverlayMounter,
    claude_auth_isolation_label,
    default_overlay_mounter,
    force_copy_isolation_requested,
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
def test_pinned_upper_recovers_across_host_metadata_change(tmp_path: Path) -> None:
    # #382: ``ctime`` is in the signature now, so a content-identical *revert* (or a
    # metadata-preserving edit) no longer reproduces an old signature/base name. The
    # pin-based recovery path replaces that fragile recompute-and-match: a surviving
    # ``upper`` remounts against the *pinned* base regardless of host churn.
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

    settings = host_home / ".claude" / "settings.json"

    # Provision 1: overlay succeeds against the host-content-A base and records the
    # ``base.signature`` pin; the agent mutates the writable ``upper``.
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    mounter = BaseMismatchMounter(allowed_base=base_a)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_meta",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_meta" / "claude"
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # Provision 2: the overlay is torn down and the operator makes a
    # metadata-preserving edit — same length, mtime restored — so only ``ctime``
    # moves. The new signature differs (ctime is in the key), so a recomputed base
    # would mismatch the surviving upper; but recovery flows through the pin, which
    # records base_a exactly and is immune to host churn.
    mounter.mounted.clear()
    mtime_ns = settings.stat().st_mtime_ns
    settings.write_text('{"theme": "DARK"}\n')  # same length, content changed
    os.utime(settings, ns=(mtime_ns, mtime_ns))
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a  # ctime bump alone changed the signature

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_meta",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    # The surviving upper remounted against the *pinned* base_a (not the recomputed
    # base_b), recovering the agent's mutations — ctime-robust via the pin.
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
def test_teardown_marker_write_failure_clears_overlay_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A capable unmount whose ``.overlay-unmounted`` marker write fails (e.g.
    # ENOSPC) must not strand the auth dir: the worker's terminal-runtime-release
    # sweep is one-shot, so no later capable sweep re-writes the marker, and a
    # capability-less GC would then see ``upper`` without a marker and skip the
    # auth delete forever. Falling back to removing the overlay scratch
    # (``upper``/``work``) clears the very signal GC keys off so it can reclaim.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_nospc" / "claude"
    (claude_root / "merged").mkdir(parents=True)
    (claude_root / "upper").mkdir()
    (claude_root / "work").mkdir()
    mounter = FakeOverlayMounter(supported=True)
    mounter.mounted.add(claude_root / "merged")

    real_write_text = Path.write_text

    def _failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == _OVERLAY_UNMOUNTED_MARKER:
            raise OSError("No space left on device")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _failing_write_text)

    with capture_logs() as logs:
        teardown_workspace_auth_overlay(
            work_dir=work_dir, workspace_id="ws_nospc", overlay_mounter=mounter
        )

    assert mounter.unmounts == [claude_root / "merged"]
    assert not (claude_root / _OVERLAY_UNMOUNTED_MARKER).exists()
    # The scratch GC keys off is gone, so a later capability-less teardown is a
    # clean no-op instead of an indefinite Unverifiable failure.
    assert not (claude_root / "upper").exists()
    assert not (claude_root / "work").exists()
    # A marker-write fault is logged with its own reason code, never the
    # capability-gap code ``UNMOUNT_INCAPABLE`` — the process here is capable.
    assert any(
        entry.get("reason_code") == _CLAUDE_AUTH_OVERLAY_MARKER_WRITE_FAILED for entry in logs
    )
    assert not any(
        entry.get("reason_code") == _CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE for entry in logs
    )

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_nospc",
        overlay_mounter=mounter,
        capability_probe=lambda: False,
    )


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
def test_teardown_capable_not_mounted_copy_fallback_skips_marker(tmp_path: Path) -> None:
    # A copy-fallback workspace (``AWF_CLAUDE_AUTH_FORCE_COPY``) never built an
    # overlay ``upper``. A capable teardown must not drop a ``.overlay-unmounted``
    # marker there: GC's capability-less path only consults the marker when
    # ``upper`` exists, so the marker would be meaningless on-disk noise.
    work_dir = tmp_path / "work"
    claude_root = work_dir / "auth" / "ws_copy" / "claude"
    (claude_root / ".claude").mkdir(parents=True)
    mounter = FakeOverlayMounter(supported=True)

    teardown_workspace_auth_overlay(
        work_dir=work_dir,
        workspace_id="ws_copy",
        overlay_mounter=mounter,
        capability_probe=lambda: True,
    )

    assert mounter.unmounts == []
    assert not (claude_root / _OVERLAY_UNMOUNTED_MARKER).exists()


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
def test_shared_base_build_lock_open_failure_cleans_staging_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    real_open = auth_mounts_mod.os.open

    def _open_fails(path: object, *args: object, **kwargs: object) -> int:
        # Only the staging build-lock open fails (disk full while creating the
        # lock file); all other ``os.open`` calls proceed normally.
        if str(path).endswith(_CLAUDE_BASE_BUILD_LOCK_NAME):
            raise OSError("No space left on device")
        return real_open(path, *args, **kwargs)

    # ``os.open`` failing before the lock is acquired must still reclaim the
    # ``mkdtemp`` staging dir via the ``finally`` rather than orphaning it, and
    # provisioning degrades to the legacy full copy.
    monkeypatch.setattr(auth_mounts_mod.os, "open", _open_fails)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_lock_open_fail",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_lock_open_fail" / "claude"
    # Legacy full copy took over after the shared-base build failed.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
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
    # under ``_shared`` whose build lock is unheld (the kernel freed it on the kill),
    # so it is reaped regardless of age.
    stale = shared_root / ".claude-base-orphan"
    (stale / ".claude").mkdir(parents=True)
    (stale / ".claude" / "blob").write_text("x" * 4096)
    (stale / _CLAUDE_BASE_BUILD_LOCK_NAME).write_text("")
    # A concurrent provision's in-progress staging dir holds a live ``flock`` on its
    # ``.build.lock`` — duration-independent liveness — so it must survive.
    live = shared_root / ".claude-base-live"
    live.mkdir()
    live_lock_fd = os.open(live / _CLAUDE_BASE_BUILD_LOCK_NAME, os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(live_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # A staging entry that vanishes mid-check (dangling symlink) is skipped, never
    # fatal: ``rmtree(ignore_errors=True)`` cannot remove a symlink, so it survives.
    dangling = shared_root / ".claude-base-dangle"
    dangling.symlink_to(shared_root / "missing-target")

    try:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_reap",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )
    finally:
        os.close(live_lock_fd)

    # The base built normally (overlay mount uses it as lowerdir) and the lock-free
    # orphan is gone, while the lock-held live staging dir and the unresolvable
    # symlink are left untouched.
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
    (stale / _CLAUDE_BASE_BUILD_LOCK_NAME).write_text("")

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

    # A crash-orphaned staging dir stranded next to the existing base, its build
    # lock unheld so GC-free liveness marks it reapable.
    stale = shared_root / ".claude-base-orphan"
    (stale / ".claude").mkdir(parents=True)
    (stale / ".claude" / "blob").write_text("x" * 4096)
    (stale / _CLAUDE_BASE_BUILD_LOCK_NAME).write_text("")

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
def test_isolation_label_reports_copy_under_force_copy_request() -> None:
    # A force-copy request (bootstrap propagation preflight on a non-propagating
    # host) flips the worker to the copy fallback even while overlayfs stays
    # advertised. The label must fold in the same signal so readiness/status report
    # per_workspace_copy and not overstate the isolation/disk posture as overlay.
    assert (
        claude_auth_isolation_label(
            overlay_filesystem_available=lambda: True,
            force_copy_requested=lambda: True,
        )
        == "per_workspace_copy"
    )
    assert (
        claude_auth_isolation_label(
            overlay_filesystem_available=lambda: True,
            force_copy_requested=lambda: False,
        )
        == "per_workspace_overlay"
    )


@pytest.mark.unit
def test_force_copy_isolation_requested_public_wrapper_reads_passed_env() -> None:
    # The public wrapper is the stable cross-package entrypoint (used by
    # ``service.provider_readiness``); it mirrors the private probe's truthiness
    # set over the *passed* environ so a force-copy host is reported correctly.
    assert force_copy_isolation_requested({"AWF_CLAUDE_AUTH_FORCE_COPY": "true"}) is True
    assert force_copy_isolation_requested({"AWF_CLAUDE_AUTH_FORCE_COPY": "off"}) is False
    assert force_copy_isolation_requested({}) is False


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
def test_subprocess_overlay_mounter_active_lowerdir_multi_lower_returns_primary(
    tmp_path: Path,
) -> None:
    # A multi-layer ``lowerdir=primary:secondary`` must resolve to the first/primary
    # lower, not the whole colon-joined string treated as one directory path — which
    # would never resolve to a shared base and would silently drop the live-mount pin.
    merged = tmp_path / "merged"
    primary = tmp_path / "sig" / ".claude"
    secondary = tmp_path / "other-lower"
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {merged} overlay rw,lowerdir={primary}:{secondary},upperdir=/u,workdir=/w 0 0\n"
    )
    mounter = _SubprocessOverlayMounter(proc_mounts=proc_mounts)
    assert mounter.active_lowerdir(merged) == primary


@pytest.mark.unit
def test_subprocess_overlay_mounter_active_lowerdir_empty_lowerdir_returns_none(
    tmp_path: Path,
) -> None:
    merged = tmp_path / "merged"
    proc_mounts = tmp_path / "mounts"
    # An empty ``lowerdir=`` value yields no usable lower, so there is nothing to pin.
    proc_mounts.write_text(f"overlay {merged} overlay rw,lowerdir=,upperdir=/u,workdir=/w 0 0\n")
    mounter = _SubprocessOverlayMounter(proc_mounts=proc_mounts)
    assert mounter.active_lowerdir(merged) is None


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


# ---------------------------------------------------------------------------
# #379 — duration-independent in-progress staging lock for the reaper.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inprogress_staging_lock_survives_arbitrarily_long_build(tmp_path: Path) -> None:
    # A live builder holds an exclusive ``flock`` on its ``.build.lock``. Even with a
    # staging mtime backdated a full day past the old 1 h bound, the reaper must not
    # touch it — liveness, not elapsed time, now decides reapability.
    base_root = tmp_path / "claude-base"
    staging = base_root / "sig" / ".claude-base-live"
    (staging / ".claude").mkdir(parents=True)
    (staging / ".claude" / "blob").write_text("x" * 4096)
    lock_fd = os.open(staging / _CLAUDE_BASE_BUILD_LOCK_NAME, os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    old = staging.stat().st_mtime - 86400
    os.utime(staging, (old, old))

    try:
        # A separate ``open`` of a held lock is denied even in one process, so the
        # liveness probe reports the live builder.
        assert _claude_base_staging_build_is_live(staging) is True
        _reap_stale_claude_base_staging(base_root)
    finally:
        os.close(lock_fd)

    assert staging.exists()


@pytest.mark.unit
def test_orphaned_staging_without_live_lock_is_reaped(tmp_path: Path) -> None:
    # The ``.build.lock`` exists but no process holds it (the builder crashed; the
    # kernel freed the lock). The reaper acquires it and reaps the orphan regardless
    # of age.
    base_root = tmp_path / "claude-base"
    staging = base_root / "sig" / ".claude-base-orphan"
    (staging / ".claude").mkdir(parents=True)
    (staging / _CLAUDE_BASE_BUILD_LOCK_NAME).write_text("")

    assert _claude_base_staging_build_is_live(staging) is False
    _reap_stale_claude_base_staging(base_root)

    assert not staging.exists()


@pytest.mark.unit
def test_staging_without_lock_file_is_reaped(tmp_path: Path) -> None:
    # A pre-upgrade staging dir (older builds locked nothing) has no ``.build.lock``;
    # with no live builder protecting it, it is reaped.
    base_root = tmp_path / "claude-base"
    staging = base_root / "sig" / ".claude-base-preupgrade"
    (staging / ".claude").mkdir(parents=True)

    assert _claude_base_staging_build_is_live(staging) is False
    _reap_stale_claude_base_staging(base_root)

    assert not staging.exists()


@pytest.mark.unit
def test_reaper_skips_staging_that_vanishes_midcheck(tmp_path: Path) -> None:
    # A staging dir whose path no longer resolves (a concurrent reap or a winning
    # ``replace`` removed it) is treated as not-live, and the reaper sweep is a
    # harmless no-op — never fatal.
    base_root = tmp_path / "claude-base"
    (base_root / "sig").mkdir(parents=True)
    vanished = base_root / "sig" / ".claude-base-gone"

    assert _claude_base_staging_build_is_live(vanished) is False
    _reap_stale_claude_base_staging(base_root)  # no staging entries: does not raise


# ---------------------------------------------------------------------------
# #381 — reconcile fallback edits into the surviving overlay ``upper``.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fallback_edit_reconciled_into_remounted_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # Provision 1: overlay succeeds; the agent writes overlay-only data in ``upper``.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reconcile",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_reconcile" / "claude"
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    (claude_root / "upper" / "agent_note.txt").write_text("overlay-era note\n")

    # Provision 2: teardown, then a transient remount failure degrades to a *fresh*
    # legacy copy. The agent then mutates a baseline file in that legacy copy (a
    # fallback edit) with a strictly-newer mtime than the base's copy.
    mounter.mounted.clear()
    mounter._mount_error = OSError("transient remount failure")
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reconcile",
        host_env={},
        overlay_mounter=mounter,
    )
    legacy_settings = claude_root / ".claude" / "settings.json"
    assert legacy_settings.is_file()
    legacy_settings.write_text('{"theme": "fallback-edited"}\n')
    base_mtime_ns = (base / "settings.json").stat().st_mtime_ns
    newer_ns = base_mtime_ns + 1_000_000_000
    os.utime(legacy_settings, ns=(newer_ns, newer_ns))

    # Provision 3: the mount works again. Before the stale legacy copy is reaped, the
    # strictly-newer fallback edit is reconciled into ``upper`` so the remounted
    # overlay does not shadow then drop it.
    mounter.mounted.clear()
    mounter._mount_error = None
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reconcile",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The fallback edit is forwarded *through* the live ``merged`` mount (newer legacy
    # wins over an absent upper entry), never poked straight into ``upper`` under the
    # live overlay. In production overlayfs copies it up into ``upper`` coherently; the
    # fake mounter has no real copy-up, so here it lands in ``merged``. The agent's
    # overlay-era data already in ``upper`` is untouched.
    assert (
        claude_root / "merged" / "settings.json"
    ).read_text() == '{"theme": "fallback-edited"}\n'
    assert (claude_root / "upper" / "agent_note.txt").read_text() == "overlay-era note\n"
    # The stale legacy copy is reaped after reconciliation.
    assert not (claude_root / ".claude").exists()


@pytest.mark.unit
def test_fresh_legacy_copy_is_not_copied_into_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # Provision 1: overlay; the agent writes overlay-only data in ``upper``.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_fresh",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_fresh" / "claude"
    (claude_root / "upper" / "agent_note.txt").write_text("overlay-era note\n")

    # Provision 2: teardown + transient failure → a *fresh, unedited* legacy copy
    # whose every file mtime equals the base (``copy2`` preserved host mtimes).
    mounter.mounted.clear()
    mounter._mount_error = OSError("transient remount failure")
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_fresh",
        host_env={},
        overlay_mounter=mounter,
    )
    assert (claude_root / ".claude" / "settings.json").is_file()

    # Provision 3: remount. Reconciliation must copy *nothing* — the fresh baseline
    # matches the base mtime, so it is not a fallback edit and the overlay's disk
    # savings (an empty-ish upper) are preserved.
    mounter.mounted.clear()
    mounter._mount_error = None
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_fresh",
        host_env={},
        overlay_mounter=mounter,
    )

    # No baseline file leaked into the agent's view (``merged``) or into ``upper``;
    # only the agent's overlay-era data remains and the stale legacy copy is reaped.
    assert not (claude_root / "merged" / "settings.json").exists()
    assert not (claude_root / "merged" / "skills").exists()
    assert not (claude_root / "upper" / "settings.json").exists()
    assert (claude_root / "upper" / "agent_note.txt").read_text() == "overlay-era note\n"
    assert not (claude_root / ".claude").exists()


# ---------------------------------------------------------------------------
# #382 — ``st_ctime_ns`` in the host signature catches metadata-preserving edits.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_signature_tracks_metadata_preserving_edit(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    before = _host_claude_signature(host_home)

    settings = host_home / ".claude" / "settings.json"
    mtime_ns = settings.stat().st_mtime_ns
    new_content = '{"theme": "DARK"}\n'  # same length as the seeded '{"theme": "dark"}\n'
    assert len(new_content) == len('{"theme": "dark"}\n')
    settings.write_text(new_content)
    # Restore the original mtime; size and mode are unchanged, so only ``ctime`` moves.
    os.utime(settings, ns=(mtime_ns, mtime_ns))
    assert settings.stat().st_mtime_ns == mtime_ns

    after = _host_claude_signature(host_home)
    # Size+mtime+mode alone would be unchanged; ``ctime`` flags the hidden rewrite.
    assert after != before


# ---------------------------------------------------------------------------
# iter_overlay_lowerdirs — enumerate every live overlay lowerdir (for GC-B).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_iter_overlay_lowerdirs_collects_all_overlays(tmp_path: Path) -> None:
    base_one = tmp_path / "_shared" / "claude-base" / "sig1" / ".claude"
    base_two = tmp_path / "_shared" / "claude-base" / "sig2" / ".claude"
    # A space in a path is octal-escaped in ``/proc/mounts`` and must be decoded.
    spaced = tmp_path / "weird dir" / ".claude"
    escaped_spaced = str(spaced).replace(" ", "\\040")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        "proc /proc proc rw 0 0\n"
        f"overlay {tmp_path / 'm1'} overlay rw,lowerdir={base_one},upperdir=/u,workdir=/w 0 0\n"
        f"ext4 /data ext4 rw 0 0\n"
        f"overlay {tmp_path / 'm2'} overlay rw,lowerdir={base_two}:{escaped_spaced} 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {base_one, base_two, spaced}


@pytest.mark.unit
def test_iter_overlay_lowerdirs_handles_missing_and_optionless(tmp_path: Path) -> None:
    from awf.node.auth_mounts import iter_overlay_lowerdirs

    # Missing ``/proc/mounts`` → empty set.
    assert iter_overlay_lowerdirs(tmp_path / "absent") == set()

    # An overlay line carrying no ``lowerdir=`` option contributes nothing.
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(f"overlay {tmp_path / 'm'} overlay rw,upperdir=/u,workdir=/w 0 0\n")
    assert iter_overlay_lowerdirs(proc_mounts) == set()


@pytest.mark.unit
def test_unescape_proc_mount_field_decodes_backslash_last() -> None:
    # ``/proc/mounts`` encodes a literal backslash as ``\134``. A path holding a
    # backslash immediately followed by the digits ``040``/``011``/``012`` —
    # ``/foo\040bar`` — is therefore written ``/foo\134040bar``. Decoding the
    # backslash escape first would leave ``/foo\040bar`` and the space pass would
    # then corrupt it into ``/foo bar``; the non-backslash escapes must decode
    # first so a decoded backslash is never re-read as another escape.
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\134040bar") == "/foo\\040bar"
    assert _unescape_proc_mount_field("/foo\\134011bar") == "/foo\\011bar"
    assert _unescape_proc_mount_field("/foo\\134012bar") == "/foo\\012bar"
    # The plain escapes still decode correctly.
    assert _unescape_proc_mount_field("a\\040b\\011c\\012d\\134e") == "a b\tc\nd\\e"


@pytest.mark.unit
def test_reconcile_skips_unstattable_legacy_file(tmp_path: Path) -> None:
    # A legacy entry whose ``stat`` fails (a dangling symlink) is skipped, never
    # fatal, and copies nothing.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "dangling").symlink_to(tmp_path / "missing-target")

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    assert list(merged.iterdir()) == []
    assert list(upper.iterdir()) == []


@pytest.mark.unit
def test_reconcile_upper_wins_ties(tmp_path: Path) -> None:
    # The base lacks the file (so it reads as a fallback edit), but ``upper`` already
    # holds an equal-or-newer version — the agent's authoritative overlay-era change.
    # Only a *strictly newer* legacy edit may overwrite it, so the tie leaves ``upper``
    # untouched and nothing is forwarded through ``merged``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")
    (upper / "f").write_text("upper edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    os.utime(upper / "f", ns=(legacy_mtime_ns, legacy_mtime_ns))  # equal mtimes

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    assert (upper / "f").read_text() == "upper edit\n"
    assert not (merged / "f").exists()


@pytest.mark.unit
def test_reconcile_compares_upper_symlink_own_mtime_not_target(tmp_path: Path) -> None:
    # ``upper`` is agent-controlled and can hold a planted symlink. The generation
    # comparison for the "upper wins ties" rule must use the overlay entry's *own*
    # mtime (``lstat``), not the symlink target's (``stat``): the target's mtime is
    # unrelated to when the agent made the overlay-era change. Here the upper symlink's
    # own mtime is older than the legacy fallback edit while its target is newer, so the
    # strictly-newer legacy edit must win and be forwarded. With the buggy ``stat`` the
    # target's newer mtime would wrongly suppress the edit.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    # The symlink target carries a *newer* mtime — it must NOT be what we compare.
    target = outside / "target"
    target.write_text("newer target\n")
    os.utime(target, ns=(legacy_mtime_ns + 1_000_000, legacy_mtime_ns + 1_000_000))
    # The agent-planted upper symlink, whose own (lstat) mtime predates the legacy edit.
    (upper / "f").symlink_to(target)
    os.utime(
        upper / "f",
        ns=(legacy_mtime_ns - 1_000_000, legacy_mtime_ns - 1_000_000),
        follow_symlinks=False,
    )

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # The strictly-newer legacy edit wins and is forwarded through ``merged``.
    assert (merged / "f").read_text() == "legacy edit\n"


@pytest.mark.unit
def test_reconcile_per_file_copy_error_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-file copy failure is best-effort: it is swallowed so reconciliation never
    # blocks provisioning.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("fallback edit\n")  # base lacks it, upper lacks it

    def _copy_fails(src: object, dst: object, *args: object, **kwargs: object) -> object:
        raise OSError("No space left on device")

    monkeypatch.setattr(auth_mounts_mod.shutil, "copy2", _copy_fails)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    assert not (merged / "f").exists()
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_writes_through_merged_not_directly_into_upper(tmp_path: Path) -> None:
    # A genuine fallback edit (absent from base, absent from upper) is forwarded
    # *through* the live ``merged`` mount, not poked straight into the underlying
    # ``upper`` dir — overlayfs treats external writes to a live overlay's upper tree
    # as undefined behavior that can read back stale/invisible through ``merged``. The
    # real kernel then copies the write up into ``upper``; the test has no real overlay,
    # so the file lands in ``merged`` and ``upper`` stays untouched, proving the write
    # target is ``merged``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("fallback edit\n")

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # Forwarded through ``merged`` (including the copied-up parent dir)...
    assert (merged / "nested" / "f").read_text() == "fallback edit\n"
    # ...never written directly into ``upper`` under the live overlay.
    assert not (upper / "nested").exists()


@pytest.mark.unit
def test_reconcile_refuses_symlinked_destination(tmp_path: Path) -> None:
    # The live ``merged`` overlay's upper layer is written by the (untrusted) agent.
    # A prior overlay run may have left an agent-created symlink at the destination
    # ``rel``. ``shutil.copy2`` follows destination symlinks by default, so as the root
    # worker it would write the legacy file's content *through* the link to a target
    # outside the ``.claude`` tree — an arbitrary root-write primitive. The reconcile
    # must refuse to follow it and leave the out-of-tree target untouched.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "secret"
    secret.write_text("original\n")
    # A genuine fallback edit (absent from base and upper) the attacker wants written.
    (legacy / "f").write_text("attacker payload\n")
    # The agent-planted destination symlink, surviving in the live merged mount.
    (merged / "f").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # copy2 must NOT have followed the symlink to overwrite the out-of-tree target.
    assert secret.read_text() == "original\n"
    # The planted symlink is skipped, never materialized into a real file.
    assert (merged / "f").is_symlink()


@pytest.mark.unit
def test_reconcile_skips_source_symlink_so_target_is_not_disclosed(tmp_path: Path) -> None:
    # A *fresh* legacy copy is materialized via ``copytree(symlinks=False)`` and so
    # never contains symlinks; any symlink in the legacy tree was planted by the
    # (untrusted) agent during the fallback session (the legacy copy is mounted ``rw``
    # into the agent). ``shutil.copy2`` follows *source* symlinks by default, so as the
    # root worker reconciling later it would read the link target — possibly outside the
    # ``.claude`` tree — and write its contents into the agent-visible overlay, an
    # arbitrary root-read primitive. The destination guard only blocks writes *through* a
    # dest link, so a source link must be skipped: read nothing, copy nothing.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "root-only-secret"
    secret.write_text("root-only contents\n")
    # The agent plants a legacy-copy symlink at a path absent from base/upper pointing at
    # an out-of-tree root-readable target, so it reads as a brand-new "fallback edit".
    (legacy / "stolen").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # The link target's contents must NOT be surfaced through the overlay (merged/upper).
    assert not (merged / "stolen").exists()
    assert not (upper / "stolen").exists()
    # The out-of-tree secret is untouched and was never read into the tree.
    assert secret.read_text() == "root-only contents\n"


@pytest.mark.unit
def test_reconcile_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    # The same escape, planted one level up: an agent-created symlink at a *parent*
    # directory of ``rel``. ``mkdir(parents=True)`` would traverse through it and
    # ``copy2`` would then write inside the escaped directory. The reconcile must
    # refuse the symlinked parent and write nothing outside the tree.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("attacker payload\n")
    # The agent planted ``nested`` as a symlink to an out-of-tree dir in the upper layer.
    (merged / "nested").symlink_to(outside)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # The escaped directory must not receive the forwarded file.
    assert not (outside / "f").exists()
    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_reconcile_refuses_directory_destination(tmp_path: Path) -> None:
    # The escape via a *directory* destination: the agent (writing the live ``merged``
    # upper layer) created a directory at the same relative path as a legacy regular
    # file, holding an inner symlink whose name matches the legacy basename. ``shutil.copy2``
    # treats a directory ``dst`` as a *containing* directory and writes ``dst / basename(src)``
    # — i.e. through that planted inner symlink — letting the root copy escape the ``.claude``
    # tree. ``_safe_overlay_dest`` only rejected symlinks, so the directory dest slipped
    # through. It must refuse the directory destination and write nothing outside the tree.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "secret"
    secret.write_text("original\n")
    # A genuine fallback edit (absent from base and upper) the attacker wants written.
    (legacy / "f").write_text("attacker payload\n")
    # The agent planted ``merged/f`` as a directory containing ``f`` -> out-of-tree secret;
    # ``copy2`` into the directory targets ``merged/f/f`` and would follow that inner link.
    (merged / "f").mkdir()
    (merged / "f" / "f").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # copy2 must NOT have written through the inner symlink to the out-of-tree target.
    assert secret.read_text() == "original\n"
    # The planted inner symlink is left intact, never materialized into a real file.
    assert (merged / "f" / "f").is_symlink()


@pytest.mark.unit
def test_reconcile_skips_source_fifo_so_root_worker_cannot_block(tmp_path: Path) -> None:
    # The agent can ``mkfifo`` in the ``rw`` legacy copy during the fallback session. A
    # FIFO ``stat``s fine, so ``_safe_mtime_ns`` would treat it as a brand-new fallback
    # edit (absent from base/upper) — but ``shutil.copy2`` then ``open``s the source for
    # reading, and a FIFO with no peer writer blocks the root worker *indefinitely*,
    # hanging provisioning. The source must be skipped before any open: a regular source
    # file is required. (If this regressed, the test itself would hang.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    os.mkfifo(legacy / "pipe")

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # Nothing was read from or forwarded for the FIFO, and it is left intact.
    assert list(merged.iterdir()) == []
    assert list(upper.iterdir()) == []
    assert stat.S_ISFIFO((legacy / "pipe").stat().st_mode)


@pytest.mark.unit
def test_reconcile_refuses_fifo_destination(tmp_path: Path) -> None:
    # The agent (writing the live ``merged`` upper layer) planted a FIFO at the same
    # relative path as a legacy regular-file edit. ``shutil.copy2`` ``open``s ``dst`` for
    # writing, and a FIFO with no peer reader blocks the root worker indefinitely, hanging
    # provisioning. ``_safe_overlay_dest`` only rejected symlinks and directories, so the
    # special-file dest slipped through. It must refuse the FIFO destination and forward
    # nothing. (If this regressed, the test itself would hang.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    # A genuine fallback edit (absent from base and upper).
    (legacy / "f").write_text("fallback edit\n")
    # The agent-planted destination FIFO surviving in the live merged mount.
    os.mkfifo(merged / "f")

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # The FIFO is left intact, never opened/clobbered, and nothing reached ``upper``.
    assert stat.S_ISFIFO((merged / "f").stat().st_mode)
    assert not (upper / "f").exists()
