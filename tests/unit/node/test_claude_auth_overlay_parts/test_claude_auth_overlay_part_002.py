"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 2).

Continuation of :mod:`test_claude_auth_overlay_part_001`; the shared
:class:`FakeOverlayMounter` and ``_seed_host_claude`` helper are imported from
that part so every test in this package exercises the same fakes.
"""

from __future__ import annotations

import fcntl
import os
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
    _has_cap_mknod,
    _has_cap_sys_admin,
    _host_claude_signature,
    _overlay_filesystem_available,
    _reap_stale_claude_base_staging,
    _shared_claude_base_dir,
    _SubprocessOverlayMounter,
    claude_auth_isolation_label,
    default_overlay_mounter,
    force_copy_isolation_requested,
    overlay_path_has_reserved_chars,
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
def test_isolation_label_reports_copy_when_overlay_path_unsupported() -> None:
    # On a host whose work dir carries a ``,`` or ``:`` the overlay ``-o`` payload
    # cannot encode it, so *every* mount degrades to the per-workspace copy. The
    # label must fold in that deterministic host-level signal — exactly as it does
    # force-copy — or readiness/status would overstate per_workspace_overlay while
    # the worker actually uses per-workspace copies.
    assert (
        claude_auth_isolation_label(
            overlay_filesystem_available=lambda: True,
            overlay_path_unsupported=lambda: True,
        )
        == "per_workspace_copy"
    )
    assert (
        claude_auth_isolation_label(
            overlay_filesystem_available=lambda: True,
            overlay_path_unsupported=lambda: False,
        )
        == "per_workspace_overlay"
    )


@pytest.mark.unit
def test_overlay_path_has_reserved_chars_public_wrapper() -> None:
    # The public wrapper is the stable cross-package entrypoint (used by
    # ``service.provider_readiness`` to fold the reserved-chars fallback into the
    # isolation label); it mirrors the private probe's truthiness.
    assert overlay_path_has_reserved_chars(Path("/srv/awf,work")) is True
    assert overlay_path_has_reserved_chars(Path("/srv/awf:work")) is True
    assert overlay_path_has_reserved_chars(Path("/srv/awf/work")) is False


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
def test_has_cap_mknod_parses_proc_status(tmp_path: Path) -> None:
    # CAP_MKNOD is capability bit 27; mirror the CAP_SYS_ADMIN probe. ``0xfffffff``
    # (28 low bits set) holds bit 27; ``0x7ffffff`` (27 low bits) does not.
    granted = tmp_path / "granted"
    granted.write_text("Name:\tworker\nCapEff:\t000000000fffffff\n")
    denied = tmp_path / "denied"
    denied.write_text("Name:\tagent\nCapEff:\t0000000007ffffff\n")
    no_line = tmp_path / "no-line"
    no_line.write_text("Name:\tagent\n")
    bad_hex = tmp_path / "bad-hex"
    bad_hex.write_text("CapEff:\tnot-hex\n")
    assert _has_cap_mknod(granted) is True
    assert _has_cap_mknod(denied) is False
    assert _has_cap_mknod(no_line) is False
    assert _has_cap_mknod(bad_hex) is False
    assert _has_cap_mknod(tmp_path / "missing") is False


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
