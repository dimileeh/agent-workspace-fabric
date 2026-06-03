"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth.

These tests inject a fake :class:`OverlayMounter` so the overlay/fallback/
teardown branches are exercised without root or a real overlayfs mount. True
kernel overlay semantics are validated operationally and guarded by the
copy fallback; here we assert mount layout, base reuse, isolation, chown
routing, fallback, and unmount-before-remove.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node import auth_mounts as auth_mounts_mod
from awf.node.auth_mounts import (
    _has_cap_sys_admin,
    _overlay_filesystem_available,
    _shared_claude_base_dir,
    _SubprocessOverlayMounter,
    claude_auth_isolation_label,
    default_overlay_mounter,
    resolve_service_auth_mounts,
    teardown_workspace_auth_overlay,
)


class FakeOverlayMounter:
    """In-memory :class:`OverlayMounter` recording mount/unmount calls."""

    def __init__(
        self,
        *,
        supported: bool = True,
        mount_error: Exception | None = None,
        unmount_error: Exception | None = None,
    ) -> None:
        self._supported = supported
        self._mount_error = mount_error
        self._unmount_error = unmount_error
        self.mounts: list[dict[str, Path]] = []
        self.unmounts: list[Path] = []
        self.mounted: set[Path] = set()

    def supported(self) -> bool:
        return self._supported

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        if self._mount_error is not None:
            raise self._mount_error
        self.mounts.append(
            {"lowerdir": lowerdir, "upperdir": upperdir, "workdir": workdir, "merged": merged}
        )
        self.mounted.add(Path(merged))

    def unmount(self, target: Path) -> None:
        self.unmounts.append(Path(target))
        if self._unmount_error is not None:
            raise self._unmount_error
        self.mounted.discard(Path(target))

    def is_mounted(self, target: Path) -> bool:
        return Path(target) in self.mounted


def _seed_host_claude(host_home: Path) -> None:
    claude = host_home / ".claude"
    (claude / "skills" / "demo").mkdir(parents=True)
    (claude / "skills" / "demo" / "SKILL.md").write_text("# demo skill\n")
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    (claude / "projects" / "repo").mkdir(parents=True)
    (claude / "projects" / "repo" / "session.jsonl").write_text('{"usage": "historical"}\n')
    (host_home / ".claude.json").write_text("{}\n")


@pytest.mark.unit
def test_overlay_happy_path_mounts_shared_base_with_per_workspace_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_overlay",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_overlay" / "claude"
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert len(mounter.mounts) == 1
    call = mounter.mounts[0]
    assert call["lowerdir"] == _shared_claude_base_dir(work_dir)
    assert call["upperdir"] == claude_root / "upper"
    assert call["workdir"] == claude_root / "work"
    assert call["merged"] == claude_root / "merged"
    # ``~/.claude.json`` stays a tiny per-workspace file copy.
    assert by_target["/home/agent/.claude.json"].source == str(claude_root / ".claude.json")


@pytest.mark.unit
def test_shared_base_built_once_and_reused_across_workspaces(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    first = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_a",
        host_env={},
        overlay_mounter=mounter,
    )
    base = _shared_claude_base_dir(work_dir)
    # A rebuild would replace ``base`` via os.replace and lose this marker; its
    # survival proves the second workspace reuses the existing base.
    marker = base / ".reuse-marker"
    marker.write_text("kept")

    second = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_b",
        host_env={},
        overlay_mounter=mounter,
    )

    assert marker.read_text() == "kept"
    assert mounter.mounts[0]["lowerdir"] == base
    assert mounter.mounts[1]["lowerdir"] == base
    first_by_target = {m.target: m for m in first}
    second_by_target = {m.target: m for m in second}
    assert first_by_target["/home/agent/.claude"].source != (
        second_by_target["/home/agent/.claude"].source
    )


@pytest.mark.unit
def test_shared_base_content_excludes_history_and_skips_dangling_links(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    host_claude = host_home / ".claude"
    (host_claude / "todos").mkdir()
    (host_claude / "shell-snapshots").mkdir()
    (host_claude / "statsig").mkdir()
    stale_skill = host_claude / "skills" / "stale"
    stale_skill.mkdir()
    (stale_skill / "SKILL.md").symlink_to(host_home / "removed" / "SKILL.md")

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_base",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    base = _shared_claude_base_dir(work_dir)
    assert (base / "skills" / "demo" / "SKILL.md").read_text() == "# demo skill\n"
    assert (base / "settings.json").read_text() == '{"theme": "dark"}\n'
    for excluded in ("projects", "todos", "shell-snapshots", "statsig"):
        assert not (base / excluded).exists()
    assert not (base / "skills" / "stale" / "SKILL.md").exists()


@pytest.mark.unit
def test_overlay_isolation_only_chowns_upper_and_work_not_merged_or_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    chowned: list[Path] = []
    monkeypatch.setattr(
        auth_mounts_mod.os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        auth_mounts_mod.os,
        "lchown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_a",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
        overlay_mounter=mounter,
    )
    base = _shared_claude_base_dir(work_dir)
    chowned.clear()  # discard the one-time base build chowns

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_b",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
        overlay_mounter=mounter,
    )

    claude_root_b = work_dir / "auth" / "ws_b" / "claude"
    # The second resolution chowns only B's writable upper/work — never the
    # shared base and never the live ``merged`` overlay mount.
    assert (claude_root_b / "upper") in chowned
    assert (claude_root_b / "work") in chowned
    assert (claude_root_b / "merged") not in chowned
    shared_root = work_dir / "auth" / "_shared"
    assert not any(path.is_relative_to(shared_root) for path in chowned)
    assert not any(path.is_relative_to(base) for path in chowned)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mounter",
    [
        FakeOverlayMounter(supported=False),
        FakeOverlayMounter(supported=True, mount_error=PermissionError("no CAP_SYS_ADMIN")),
        FakeOverlayMounter(supported=True, mount_error=subprocess.CalledProcessError(32, "mount")),
    ],
)
def test_overlay_unavailable_falls_back_to_legacy_copy(
    tmp_path: Path,
    mounter: FakeOverlayMounter,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_fallback",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_fallback" / "claude"
    # Legacy full copy: the mount source is the copied ``.claude`` tree.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_root / ".claude" / "projects").exists()
    # No stray ``merged`` mountpoint is left behind on fallback.
    assert not (claude_root / "merged").exists()
    if mounter.supported():
        assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)


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
def test_teardown_raises_and_logs_on_real_umount_error(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    merged = work_dir / "auth" / "ws_busy" / "claude" / "merged"
    merged.mkdir(parents=True)
    mounter = FakeOverlayMounter(
        supported=True, unmount_error=subprocess.CalledProcessError(32, "umount")
    )
    mounter.mounted.add(merged)

    with capture_logs() as logs, pytest.raises(subprocess.CalledProcessError):
        teardown_workspace_auth_overlay(
            work_dir=work_dir, workspace_id="ws_busy", overlay_mounter=mounter
        )
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED" for entry in logs)


@pytest.mark.unit
def test_teardown_with_default_mounter_is_noop_when_path_absent(tmp_path: Path) -> None:
    # No injected mounter exercises ``default_overlay_mounter`` + the real
    # ``os.path.ismount`` on a path that is not a mountpoint.
    teardown_workspace_auth_overlay(work_dir=tmp_path / "work", workspace_id="ws_absent")


@pytest.mark.unit
def test_shared_base_build_loses_race_and_reuses_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    base = _shared_claude_base_dir(work_dir)

    def _lost_race(self: Path, target: Path) -> None:
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
def test_shared_base_is_never_under_a_workspace_auth_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    base = _shared_claude_base_dir(work_dir)
    auth_root = work_dir / "auth"
    # The base lives under ``auth/_shared`` and never under any concrete
    # ``auth/<workspace_id>`` dir, so GC candidate enumeration cannot reap it.
    assert base.is_relative_to(auth_root / "_shared")
    for workspace_id in ("ws_a", "ws_b", "_shared0"):
        assert not base.is_relative_to(auth_root / workspace_id)


@pytest.mark.unit
def test_isolation_label_reflects_overlay_support() -> None:
    assert (
        claude_auth_isolation_label(overlay_mounter=FakeOverlayMounter(supported=True))
        == "per_workspace_overlay"
    )
    assert (
        claude_auth_isolation_label(overlay_mounter=FakeOverlayMounter(supported=False))
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
