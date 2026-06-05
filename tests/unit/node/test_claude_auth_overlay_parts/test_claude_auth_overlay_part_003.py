"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 3).

Continuation of :mod:`test_claude_auth_overlay_part_002`; covers the
``/proc/mounts`` lowerdir-discovery helpers (now in
:mod:`awf.node.auth_mounts_overlay`, re-exported through ``auth_mounts``) and the
fallback-edit reconcile branches. The shared :class:`FakeOverlayMounter` and
``_seed_host_claude`` helper are imported from :mod:`test_claude_auth_overlay_part_001`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# Patch the module that defines the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node import auth_mounts_overlay_copy as overlay_copy_mod
from awf.node.auth_mounts import (
    _reconcile_fallback_edits_into_upper,
    _safe_overlay_whiteout,
)

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter as FakeOverlayMounter,
)
from .test_claude_auth_overlay_part_001 import (
    _seed_host_claude as _seed_host_claude,
)


def _recording_mknod(recorded: list[dict[str, object]]):
    """Return a fake ``os.mknod`` recording its args and creating a placeholder.

    A real overlayfs whiteout is a char device 0,0, which ``mknod`` can only create
    with ``CAP_MKNOD``/root — unavailable in unit tests. The fake records the mode /
    device / leaf name the production code requested and materializes a 0-byte
    placeholder at the same ``dir_fd``-relative location so callers can assert the
    whiteout landed in ``upper`` without real privileges.
    """

    real_open = os.open

    def _fake_mknod(
        path: object, mode: int = 0o600, device: int = 0, *, dir_fd: int | None = None
    ) -> None:
        recorded.append({"name": os.fspath(path), "mode": mode, "device": device})
        fd = real_open(os.fspath(path), os.O_WRONLY | os.O_CREAT, 0o000, dir_fd=dir_fd)
        os.close(fd)

    return _fake_mknod


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
def test_iter_overlay_lowerdirs_decodes_octal_colon_in_path(tmp_path: Path) -> None:
    # A literal colon in a lowerdir path is octal-escaped as ``\072`` by overlayfs
    # (its raw ``:`` is reserved for joining layers), so the split-on-``:`` keeps the
    # path whole and the escape must be decoded back to ``:``. Otherwise GC-B's
    # protected set holds ``...\072...`` instead of the real path and reaps a base
    # still backing a live overlay. (PRRT_kwDOSJAM6s6HN8ld)
    colon_base = Path("/srv/weird:dir/_shared/claude-base/sig/.claude")
    escaped = str(colon_base).replace(":", "\\072")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {tmp_path / 'm'} overlay rw,lowerdir={escaped},upperdir=/u,workdir=/w 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {colon_base}


@pytest.mark.unit
def test_iter_overlay_lowerdirs_decodes_octal_comma_in_path(tmp_path: Path) -> None:
    # The mount-option field is comma-separated, so a literal comma in a lowerdir
    # path is octal-escaped as ``\054`` by overlayfs. The split-on-``,`` over the
    # options keeps the path whole, but the escape must be decoded back to ``,`` or
    # GC-B's protected set holds ``...\054...`` instead of the real path and reaps a
    # base still backing a live overlay. (PRRT_kwDOSJAM6s6HOMbH)
    comma_base = Path("/srv/weird,dir/_shared/claude-base/sig/.claude")
    escaped = str(comma_base).replace(",", "\\054")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {tmp_path / 'm'} overlay rw,lowerdir={escaped},upperdir=/u,workdir=/w 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {comma_base}


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
def test_unescape_proc_mount_field_decodes_octal_colon() -> None:
    # overlayfs uses a raw ``:`` to join layered lowerdirs, so a literal colon
    # inside a single lowerdir path is octal-escaped as ``\072`` (the split-on-``:``
    # in the lowerdir parsers therefore never tears such a path apart). The escape
    # must still be decoded back to ``:`` here, or a base path containing a colon
    # would come back as ``...\072...`` and fail the ``base_root`` match in
    # ``_protected_signature_dirs`` — letting GC-B reap a base that still backs a
    # live overlay. (PRRT_kwDOSJAM6s6HN8ld)
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\072bar") == "/foo:bar"
    # A literal backslash followed by the text ``072`` is written ``\134072`` and
    # must round-trip to ``/foo\072bar`` (the backslash, not a decoded colon) —
    # decoding the colon escape before the backslash keeps that distinction.
    assert _unescape_proc_mount_field("/foo\\134072bar") == "/foo\\072bar"
    # A literal backslash immediately followed by a colon is ``\134\072`` and must
    # decode to a backslash then a colon.
    assert _unescape_proc_mount_field("/foo\\134\\072bar") == "/foo\\:bar"


@pytest.mark.unit
def test_unescape_proc_mount_field_decodes_octal_comma() -> None:
    # The mount-option field is comma-separated, so a literal comma inside a single
    # lowerdir path is octal-escaped as ``\054`` (the split-on-``,`` over options
    # therefore never tears such a path apart). The escape must still be decoded
    # back to ``,`` here, or a base path containing a comma would come back as
    # ``...\054...`` and fail the ``base_root`` match in ``_protected_signature_dirs``
    # — letting GC-B reap a base that still backs a live overlay. (PRRT_kwDOSJAM6s6HOMbH)
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\054bar") == "/foo,bar"
    # A literal backslash followed by the text ``054`` is written ``\134054`` and
    # must round-trip to ``/foo\054bar`` (the backslash, not a decoded comma) —
    # decoding the comma escape before the backslash keeps that distinction.
    assert _unescape_proc_mount_field("/foo\\134054bar") == "/foo\\054bar"
    # A literal backslash immediately followed by a comma is ``\134\054`` and must
    # decode to a backslash then a comma.
    assert _unescape_proc_mount_field("/foo\\134\\054bar") == "/foo\\,bar"


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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    # The fd-based safe copy streams content via ``shutil.copyfileobj``; a mid-stream
    # write failure must be swallowed so reconciliation never blocks provisioning.
    monkeypatch.setattr(auth_mounts_mod.shutil, "copyfileobj", _copy_fails)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # Best-effort: the failure is swallowed and nothing is forwarded into the live
    # overlay's underlying ``upper`` (as with a partial ``copy2``, an empty leaf may be
    # left in ``merged`` — never any content, never an exception).
    assert not (upper / "f").exists()
    assert (merged / "f").read_text() == ""


@pytest.mark.unit
def test_reconcile_preserves_destination_when_source_vanishes_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the ``_safe_overlay_copy`` ftruncate-ordering fix: if the legacy
    # source disappears in the window after the atomic source ``os.open`` would have run
    # but before its read fd is acquired, the pre-existing destination (the agent's
    # overlay-era edit, visible through the live ``merged`` mount) must be left intact,
    # never zeroed by an early ``ftruncate``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")  # base lacks it -> fallback edit
    # An older ``upper`` entry so the strictly-newer legacy edit is selected to forward.
    (upper / "f").write_text("agent overlay edit\n")
    # Pre-existing destination content seen through the live overlay's ``merged`` mount.
    (merged / "f").write_text("agent overlay edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    os.utime(upper / "f", ns=(legacy_mtime_ns - 1_000_000, legacy_mtime_ns - 1_000_000))

    real_os_open = os.open

    def _os_open_source_vanished(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # Simulate the source being removed in the window before its read fd is held.
        # The source leaf is opened by bare name (``rel.name``) relative to its descended
        # parent ``dir_fd``: it is the only read-only, non-directory ``os.open`` of ``f``
        # (the dir descents use ``O_DIRECTORY``; the destination leaf open uses ``O_CREAT``).
        if os.fspath(path) == "f" and not (flags & (os.O_DIRECTORY | os.O_CREAT)):
            raise OSError("source vanished")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _os_open_source_vanished)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The source open failed and was swallowed best-effort; the destination keeps its
    # original content instead of being truncated to empty.
    assert (merged / "f").read_text() == "agent overlay edit\n"


@pytest.mark.unit
def test_reconcile_does_not_create_empty_destination_when_source_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the source-before-destination open ordering: for a *new* fallback
    # edit (no prior ``upper``/``merged`` entry) the destination must not be opened with
    # ``O_CREAT`` before the source is validated. Otherwise a source-open failure (e.g.
    # the legacy file unlinked in the non-atomic window after the caller's ``is_file()``
    # pre-check) would leave a 0-byte regular file in the overlay upper that silently
    # shadows the base entry — a Claude config could read as empty to the agent.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")  # base/upper lack it -> new fallback edit

    real_os_open = os.open

    def _os_open_source_vanished(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # Fail only the source read leaf open — opened by bare name (``rel.name``) relative
        # to its descended parent ``dir_fd`` (the dir descents use ``O_DIRECTORY``; the
        # destination leaf open uses ``O_CREAT``). If the destination were opened first it
        # would already have created the empty file before this fires.
        if os.fspath(path) == "f" and not (flags & (os.O_DIRECTORY | os.O_CREAT)):
            raise OSError("source vanished")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _os_open_source_vanished)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # No empty file was materialised in the overlay upper to shadow the base entry.
    assert not (merged / "f").exists()


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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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
    # tree. The fd-based open of the leaf is ``O_WRONLY``, so a directory dest fails with
    # ``EISDIR``: it must refuse the directory destination and write nothing outside the tree.
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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # Nothing was read from or forwarded for the FIFO, and it is left intact.
    assert list(merged.iterdir()) == []
    assert list(upper.iterdir()) == []
    assert stat.S_ISFIFO((legacy / "pipe").stat().st_mode)


@pytest.mark.unit
def test_reconcile_refuses_fifo_destination(tmp_path: Path) -> None:
    # The agent (writing the live ``merged`` upper layer) planted a FIFO at the same
    # relative path as a legacy regular-file edit. ``shutil.copy2`` ``open``s ``dst`` for
    # writing, and a FIFO with no peer reader blocks the root worker indefinitely, hanging
    # provisioning. The fd-based open uses ``O_NONBLOCK``, so a reader-less FIFO fails with
    # ``ENXIO`` instead of blocking: it must refuse the FIFO destination and forward
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

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The FIFO is left intact, never opened/clobbered, and nothing reached ``upper``.
    assert stat.S_ISFIFO((merged / "f").stat().st_mode)
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_refuses_fifo_destination_with_a_live_reader(tmp_path: Path) -> None:
    # A FIFO with a *live reader* opens successfully even under ``O_WRONLY | O_NONBLOCK``
    # (the reader-less ``ENXIO`` guard does not catch it), so the ``S_ISREG`` ``fstat``
    # guard is what must refuse it: the root worker only ever writes a regular file, never
    # forwarding content into an agent-planted FIFO. (If this regressed, the test could
    # hang on the write — there is no peer draining the pipe.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("fallback edit\n")
    fifo = merged / "f"
    os.mkfifo(fifo)
    # Hold the read end open so the write-side open below succeeds instead of ENXIO.
    reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        _reconcile_fallback_edits_into_upper(
            legacy=legacy,
            merged=merged,
            upper=upper,
            base=base,
            host_claude=tmp_path / "host-claude",
        )
    finally:
        os.close(reader_fd)

    # The FIFO is left intact (never written through) and nothing reached ``upper``.
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_forwards_into_a_preexisting_merged_parent_dir(tmp_path: Path) -> None:
    # When the destination's parent directory already exists in the live ``merged``
    # overlay, the fd-based descent's ``os.mkdir`` raises ``FileExistsError`` and is a
    # no-op — it must still re-open the existing real directory under ``O_NOFOLLOW`` and
    # forward the edit into it (a non-symlink existing parent is not a structural
    # conflict).
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("fallback edit\n")
    # The parent dir already exists in ``merged`` (e.g. copied up by an earlier file).
    (merged / "nested").mkdir()

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert (merged / "nested" / "f").read_text() == "fallback edit\n"


@pytest.mark.unit
def test_reconcile_skips_usage_history_dirs_absent_from_base(tmp_path: Path) -> None:
    # The shared base is built with ``ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS)``, so
    # ``projects/``, ``todos/``, ``shell-snapshots/`` and ``statsig/`` never exist in it.
    # A fallback session's Claude process re-creates them and can fill ``projects/`` with
    # large transcripts; because the base lacks them they all read as ``base_mtime_ns is
    # None`` fallback edits. Reconcile must mirror the base exclusion and not forward those
    # subtrees into the overlay upper, or the shared-base disk savings are negated.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    # A genuine edit outside the usage-history dirs is still forwarded.
    (legacy / "settings.json").write_text("real fallback edit\n")
    # Agent-accumulated usage-history subtrees that the base does not track.
    for history_dir in ("projects", "todos", "shell-snapshots", "statsig"):
        (legacy / history_dir).mkdir()
        (legacy / history_dir / "big").write_text("multi-GB transcript\n")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert (merged / "settings.json").read_text() == "real fallback edit\n"
    for history_dir in ("projects", "todos", "shell-snapshots", "statsig"):
        assert not (merged / history_dir).exists()
        assert not (upper / history_dir).exists()


@pytest.mark.unit
def test_safe_overlay_copy_refuses_source_symlink_swapped_after_caller_checks(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard: the caller's ``is_symlink()``/``is_file()`` checks in
    # ``_reconcile_fallback_edits_into_upper`` are not atomic with the source open inside
    # ``_safe_overlay_copy``, and the legacy copy is ``rw`` for the agent — so it can swap
    # a checked-clean regular file for a symlink before the open. Calling
    # ``_safe_overlay_copy`` directly with a symlink source models that post-check swap:
    # the atomic ``O_NOFOLLOW`` source open must refuse it (``ELOOP``) so the root worker
    # never follows the link to read an arbitrary host path into the agent-visible tree.
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "root-only-secret"
    secret.write_text("root-only contents\n")
    # The agent swapped the checked-clean ``src_root/f`` leaf for a symlink to an
    # out-of-tree secret after the caller's checks but before this open.
    (src_root / "f").symlink_to(secret)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_root)

    # The link target's contents are never read into the destination tree. With the
    # source validated *before* the destination is opened, the refused source leaves no
    # 0-byte leaf behind at all, and the out-of-tree secret is untouched.
    assert not (merged / "f").exists()
    assert secret.read_text() == "root-only contents\n"


@pytest.mark.unit
def test_safe_overlay_copy_refuses_source_fifo_swapped_after_caller_checks(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard (FIFO variant): a ``mkfifo`` planted after the caller's
    # ``is_file()`` check must not block the root worker. The atomic source open uses
    # ``O_NONBLOCK`` and the ``S_ISREG`` ``fstat`` guard refuses the FIFO before any read.
    # (If this regressed, the test itself would hang — there is no peer writer.)
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    src_fifo = src_root / "f"
    os.mkfifo(src_fifo)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_root)

    # Nothing was read from the FIFO and it never blocked. The source is refused before
    # the destination is opened, so no dest leaf is created at all.
    assert not (merged / "f").exists()
    assert stat.S_ISFIFO(os.lstat(src_fifo).st_mode)


@pytest.mark.unit
def test_safe_overlay_copy_refuses_symlinked_source_parent_component(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard, one level up (PRRT_kwDOSJAM6s6HOvV4): the caller walks a
    # *real* directory ``src_root/nested`` and yields ``nested/f``, but the legacy copy is
    # ``rw`` for the agent, which can swap ``nested`` for ``nested -> /outside`` before the
    # source is read. Opening ``src_root / rel`` by full path would apply ``O_NOFOLLOW``
    # only to the ``f`` leaf, so the redirected ``nested`` parent would still resolve by
    # name and the root worker would read ``/outside/f`` into the agent-visible overlay —
    # an arbitrary root-read primitive. The component-by-component ``O_NOFOLLOW`` descent
    # of the source must refuse the symlinked parent (``ELOOP``) and copy nothing.
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "f"
    secret.write_text("root-only contents\n")
    # The agent planted ``nested`` as a symlink to the out-of-tree dir; the leaf ``f``
    # behind it is a genuine regular file, so only the parent guard can catch this.
    (src_root / "nested").symlink_to(outside)

    auth_mounts_mod._safe_overlay_copy(merged, Path("nested/f"), src_root)

    # The out-of-tree file is never read into the destination tree, and no dest leaf
    # (nor its parent) is materialized through the escaped parent.
    assert not (merged / "nested" / "f").exists()
    assert secret.read_text() == "root-only contents\n"


# --- #404: host-origin vs agent-origin newer legacy mtime ----------------------------


def _mkdirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir()


@pytest.mark.unit
def test_reconcile_keeps_upper_edit_over_unedited_host_changed_legacy(tmp_path: Path) -> None:
    # #404: the host ``~/.claude`` changed *after* the agent made an overlay-era ``upper``
    # edit, then a transient remount failure created a legacy copy *from that changed
    # host*. The legacy copy is byte/mtime-identical to the new host (the agent never
    # touched it) and carries a newer host mtime than the agent's older ``upper`` edit.
    # The mtime-only rule would treat it as a fallback edit and overwrite the agent's
    # overlay-era change. With the host-divergence guard it is recognised as an unedited
    # host copy and the ``upper`` edit is preserved — nothing is forwarded.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    # The agent's overlay-era edit, older than the (later) host change.
    (upper / "settings.json").write_text('{"theme": "agent-overlay-edit"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    # The host changed after that edit (newer mtime) ...
    (host / "settings.json").write_text('{"theme": "host-changed"}\n')
    os.utime(
        host / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )
    host_stat = (host / "settings.json").stat()
    # ... and the legacy copy is a byte/mtime-identical, unedited copy of that new host.
    (legacy / "settings.json").write_text('{"theme": "host-changed"}\n')
    os.utime(legacy / "settings.json", ns=(host_stat.st_mtime_ns, host_stat.st_mtime_ns))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The agent's overlay-era edit stands; the unedited host copy was not forwarded.
    assert (upper / "settings.json").read_text() == '{"theme": "agent-overlay-edit"}\n'
    assert not (merged / "settings.json").exists()


@pytest.mark.unit
def test_reconcile_still_forwards_agent_edited_legacy_diverging_from_host(tmp_path: Path) -> None:
    # #404 must not regress the tested fallback re-edit forwarding: when the agent
    # *did* edit the legacy copy during the fallback session, the legacy file diverges
    # from the live host (different content/mtime) and is strictly newer than the
    # ``upper`` edit, so it is forwarded over it exactly as before.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (upper / "settings.json").write_text('{"theme": "old-overlay-edit"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    # The live host holds one version ...
    (host / "settings.json").write_text('{"theme": "host"}\n')
    os.utime(host / "settings.json", ns=(upper_mtime_ns, upper_mtime_ns))
    # ... but the agent edited the legacy copy to something else, strictly newer.
    (legacy / "settings.json").write_text('{"theme": "agent-fallback-edit"}\n')
    os.utime(
        legacy / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The agent's strictly-newer fallback edit diverges from the host and is forwarded.
    assert (merged / "settings.json").read_text() == '{"theme": "agent-fallback-edit"}\n'


@pytest.mark.unit
def test_reconcile_forwards_agent_edit_when_host_lacks_file(tmp_path: Path) -> None:
    # When the live host does not hold the path at all, divergence cannot be disproven,
    # so a strictly-newer legacy file is treated as an agent edit and forwarded over an
    # existing ``upper`` regular file — fail safe toward preserving an edit, never
    # dropping/hiding one. (Host-lacking is the common shape the existing fallback
    # forwarding relies on.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (upper / "settings.json").write_text('{"theme": "old"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    (legacy / "settings.json").write_text('{"theme": "agent-edit"}\n')
    os.utime(
        legacy / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert (merged / "settings.json").read_text() == '{"theme": "agent-edit"}\n'


# --- #402: forward fallback-era deletions as overlayfs whiteouts ----------------------


@pytest.mark.unit
def test_reconcile_forwards_agent_deletion_as_whiteout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent deleted a Claude auth/config file present in ``base`` during the
    # fallback session; the live host still holds it unchanged (host == base), so the
    # legacy absence is a confident agent deletion. With CAP_MKNOD present it is
    # forwarded as an overlayfs whiteout (char device 0,0) into ``upper`` so the
    # deletion survives the next remount instead of the lower re-exposing it.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    base_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")  # same content -> same size
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))
    # ``legacy`` LACKS ``secret.json`` -> the agent removed it. ``upper`` is empty.

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert len(recorded) == 1
    # A char device 0,0 written at the deleted path's leaf, inside ``upper``.
    assert recorded[0]["name"] == "secret.json"
    assert recorded[0]["mode"] == stat.S_IFCHR
    assert recorded[0]["device"] == os.makedev(0, 0)
    assert (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_ambiguous_host_removed_file_is_not_whiteouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file is in ``base`` but absent from BOTH the legacy copy and the live host:
    # the host may itself have removed it, so the legacy absence is host-explained, not
    # confidently agent-attributable. Fail safe — never whiteout (the credential stays
    # visible). This is the credential-safety guarantee: ambiguity never hides a file.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")  # host lacks it entirely

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert not (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_ambiguous_host_changed_file_is_not_whiteouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file is in ``base`` and the host still has it, but the host's copy DIFFERS
    # from base (the host re-added / changed it after the base was built). The legacy
    # absence can no longer be confidently attributed to the agent. Fail safe — never
    # whiteout.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    # Host holds a *changed* version (different size), so it diverges from base.
    (host / "secret.json").write_text("token-rotated-and-longer\n")

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert not (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_deletion_not_whiteouted_without_cap_mknod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A confident agent deletion (host == base) that *would* be whiteouted, but the
    # worker lacks CAP_MKNOD. The capability fallback declines the whiteout — the
    # credential file stays visible (fail safe, never hidden) — and the gap is logged
    # once so a capability misconfiguration is diagnosable.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    base_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: False)

    with capture_logs() as logs:
        _reconcile_fallback_edits_into_upper(
            legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
        )

    # No whiteout was created; the credential stays visible through the lower.
    assert not (upper / "secret.json").exists()
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE" for entry in logs
    )


@pytest.mark.unit
def test_reconcile_deletion_skipped_when_upper_already_has_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A confident agent deletion (host == base), but ``upper`` already holds an entry
    # for the path: the agent's overlay state is authoritative, so the deletion pass
    # must not double-handle it. No whiteout is attempted and the existing entry stands.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    base_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))
    (upper / "secret.json").write_text('{"agent": "overlay-rewrote-it"}\n')

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert (upper / "secret.json").read_text() == '{"agent": "overlay-rewrote-it"}\n'


@pytest.mark.unit
def test_reconcile_skips_deletion_for_usage_history_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Usage-history dirs are excluded from the base (and so from the deletion walk):
    # even if one somehow appeared in base, the walk prunes it so it is never
    # whiteouted. A genuine non-history deletion alongside it is still forwarded.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    base_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))
    # A stray usage-history subtree in base (and host) that must never be whiteouted.
    (base / "projects").mkdir()
    (base / "projects" / "old.jsonl").write_text("history\n")
    (host / "projects").mkdir()
    (host / "projects" / "old.jsonl").write_text("history\n")

    monkeypatch.setattr(auth_mounts_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert [entry["name"] for entry in recorded] == ["secret.json"]
    assert not (upper / "projects").exists()


# --- _safe_overlay_whiteout primitive symlink safety / happy path ---------------------


@pytest.mark.unit
def test_safe_overlay_whiteout_creates_char_device_in_nested_upper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct happy-path: the whiteout descends and creates intermediate parent dirs in
    # ``upper`` with ``O_NOFOLLOW``, then mknods the char device relative to the
    # validated parent. Returns True. (Real mknod needs root, so it is faked.)
    upper = tmp_path / "upper"
    upper.mkdir()
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    assert _safe_overlay_whiteout(upper, Path("nested/deep/secret.json")) is True

    assert (upper / "nested" / "deep").is_dir()
    assert (upper / "nested" / "deep" / "secret.json").exists()
    assert recorded[0]["mode"] == stat.S_IFCHR
    assert recorded[0]["device"] == os.makedev(0, 0)


@pytest.mark.unit
def test_safe_overlay_whiteout_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    # The overlay ``upper`` is agent-controlled; a planted symlink at a parent component
    # of the whiteout path must never let the ``mknod`` escape the ``.claude`` tree. The
    # ``O_NOFOLLOW`` descent refuses the symlinked parent (``ELOOP``) and returns False,
    # writing nothing into the out-of-tree target. No real mknod is reached, so this
    # needs no privileges.
    upper = tmp_path / "upper"
    upper.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # The agent planted ``nested`` as a symlink to an out-of-tree dir in ``upper``.
    (upper / "nested").symlink_to(outside)

    assert _safe_overlay_whiteout(upper, Path("nested/secret.json")) is False

    # Nothing was created through the escaped parent.
    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_safe_overlay_whiteout_returns_false_on_mknod_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with the capability probe satisfied, a ``mknod`` that raises (e.g. ``EPERM``
    # on a hardened filesystem, ``EEXIST`` on a planted leaf) must fail safe: return
    # False without raising, so the deletion simply is not forwarded and the credential
    # stays visible.
    upper = tmp_path / "upper"
    upper.mkdir()

    def _mknod_eperm(*args: object, **kwargs: object) -> None:
        raise OSError("operation not permitted")

    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _mknod_eperm)

    assert _safe_overlay_whiteout(upper, Path("secret.json")) is False
    assert not (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_skips_legacy_file_that_becomes_unstattable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defensive: a legacy regular file that passes the ``is_file()`` guard but whose
    # subsequent ``stat`` fails (a TOCTOU unlink/permission race in the ``rw`` legacy
    # copy) is skipped, never fatal — nothing is forwarded for it.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (legacy / "f").write_text("edit\n")

    real_safe_stat = overlay_copy_mod._safe_stat

    def _stat_none_for_legacy(path: object, *, follow_symlinks: bool = True):
        if os.fspath(path) == os.fspath(legacy / "f"):
            return None
        return real_safe_stat(Path(os.fspath(path)), follow_symlinks=follow_symlinks)

    monkeypatch.setattr(auth_mounts_mod, "_safe_stat", _stat_none_for_legacy)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert not (merged / "f").exists()
    assert not (upper / "f").exists()
