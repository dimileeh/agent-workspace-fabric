"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 5).

Continuation of :mod:`test_claude_auth_overlay_part_003`; covers the
ambiguous-host / content-compare whiteout guards, the ``_safe_overlay_whiteout``
device-node primitive, and the ``_legacy_path_confidently_absent`` symlink-safe
deletion check (all in :mod:`awf.node.auth_mounts_overlay_copy`, re-exported
through ``auth_mounts``). The ``_mkdirs`` and ``_recording_mknod`` helpers are
imported from :mod:`test_claude_auth_overlay_part_003`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# Patch the module that defines the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude_reconcile as reconcile_mod
from awf.node import auth_mounts_overlay_copy as overlay_copy_mod
from awf.node.auth_mounts import (
    _reconcile_fallback_edits_into_upper,
    _safe_overlay_whiteout,
)

from .test_claude_auth_overlay_part_003 import (
    _mkdirs,
    _recording_mknod,
)


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

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert not (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_same_size_mtime_but_changed_content_is_not_whiteouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host rotated the credential to *different bytes* of the *same length* and
    # restored the original mtime (e.g. a ``touch -r`` after a same-length token
    # rewrite, or a timestamp-preserving sync). mtime + size alone read "host ==
    # base", but the live host now holds a different, currently valid credential.
    # Whiteouting would HIDE it — so the content compare must catch the divergence and
    # fail safe (never whiteout). This is the reviewer's #414 case.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token-aaaa\n")
    base_stat = (base / "secret.json").stat()
    # Same byte length, different content, and the same mtime/size as base.
    (host / "secret.json").write_text("token-bbbb\n")
    assert (host / "secret.json").stat().st_size == base_stat.st_size
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))
    # ``legacy`` LACKS ``secret.json`` -> looks like an agent deletion by stat alone.

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert not (upper / "secret.json").exists()


@pytest.mark.unit
def test_safe_files_equal_content_compares_bytes_and_fails_safe(tmp_path: Path) -> None:
    # Direct coverage for the content-equality primitive backing the whiteout guard:
    # byte-identical files compare equal; same-length-different-bytes do not; and an
    # unreadable/absent path fails safe to ``False`` ("not provably equal" -> keep the
    # credential visible) rather than raising.
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"token-aaaa\n")
    b.write_bytes(b"token-aaaa\n")
    assert overlay_copy_mod._safe_files_equal_content(a, b) is True

    b.write_bytes(b"token-bbbb\n")  # same length, different bytes
    assert overlay_copy_mod._safe_files_equal_content(a, b) is False

    # Absent path -> ``OSError`` on open -> fail safe to ``False``.
    assert overlay_copy_mod._safe_files_equal_content(a, tmp_path / "missing") is False


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

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: False)

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
def test_reconcile_logs_when_whiteout_fails_despite_cap_mknod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A confident agent deletion (host == base) with CAP_MKNOD probed available, but
    # the actual ``mknod`` is refused at the filesystem level (e.g. an ``upper`` tmpfs
    # without char-device support). The credential stays visible (fail safe), but
    # unlike the missing-capability path this would otherwise be completely silent:
    # assert the failure is surfaced once so an operator can diagnose why a confident
    # deletion was not forwarded even though the capability probe passed.
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

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)

    def _mknod_enodev(*args: object, **kwargs: object) -> None:
        raise OSError("operation not supported")

    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _mknod_enodev)

    with capture_logs() as logs:
        _reconcile_fallback_edits_into_upper(
            legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
        )

    # No whiteout landed; the credential stays visible through the lower.
    assert not (upper / "secret.json").exists()
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED" for entry in logs)
    # The capability-missing signal is distinct and must NOT fire here (cap was present).
    assert not any(
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

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
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

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
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

    monkeypatch.setattr(reconcile_mod, "_safe_stat", _stat_none_for_legacy)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert not (merged / "f").exists()
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_intermediate_legacy_dir_symlink_does_not_whiteout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #414 regression: the agent replaced a *subdirectory* with a symlink in the ``rw``
    # legacy copy during the fallback session (``legacy/subdir -> /empty``). A plain
    # ``lstat(legacy/subdir/hidden.json)`` would resolve *through* that intermediate link
    # (``lstat`` only declines to follow the *leaf*), see the file absent at the redirect
    # target, and — host still matching base — synthesise a whiteout that HIDES a
    # credential the agent never explicitly removed. The symlink-safe descent must treat
    # the redirected path as ambiguous (keep visible), symmetric with the edit walk's
    # ``os.walk(legacy, followlinks=False)``. A genuine root-level deletion in the same
    # run is still forwarded, proving the fix is surgical rather than blanket.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    empty = tmp_path / "empty"
    _mkdirs(legacy, merged, upper, base, host, empty)

    # Confident root-level deletion: present in base + host (unchanged), absent in legacy.
    (base / "secret.json").write_text("token\n")
    secret_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")
    os.utime(host / "secret.json", ns=(secret_stat.st_atime_ns, secret_stat.st_mtime_ns))

    # A credential under a subdir the agent redirected via a directory symlink in legacy.
    # host == base for it too, so *only* the intermediate symlink keeps it from being
    # (wrongly) whiteouted.
    (base / "subdir").mkdir()
    (base / "subdir" / "hidden.json").write_text("token\n")
    hidden_stat = (base / "subdir" / "hidden.json").stat()
    (host / "subdir").mkdir()
    (host / "subdir" / "hidden.json").write_text("token\n")
    os.utime(host / "subdir" / "hidden.json", ns=(hidden_stat.st_atime_ns, hidden_stat.st_mtime_ns))
    (legacy / "subdir").symlink_to(empty)  # agent-planted directory symlink

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # Only the unambiguous root-level deletion is forwarded; the symlink-occluded path is
    # never whiteouted (the credential stays visible after the remount).
    assert [entry["name"] for entry in recorded] == ["secret.json"]
    assert (upper / "secret.json").exists()
    assert not (upper / "subdir" / "hidden.json").exists()


# --- _legacy_path_confidently_absent primitive (#414 symlink-safe deletion check) ------


@pytest.mark.unit
def test_legacy_path_confidently_absent_true_for_absent_leaf_under_real_dirs(
    tmp_path: Path,
) -> None:
    # A leaf genuinely missing under a chain of real directories is a confident deletion.
    root = tmp_path / "legacy"
    (root / "sub").mkdir(parents=True)
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("sub/secret.json")) is True


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_when_leaf_present(tmp_path: Path) -> None:
    # The leaf exists (a regular file): present, not a deletion.
    root = tmp_path / "legacy"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "secret.json").write_text("x")
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("sub/secret.json")) is False


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_for_intermediate_symlink(tmp_path: Path) -> None:
    # The bug shape: an agent-planted *intermediate* directory symlink. ``lstat`` would
    # resolve through it and misread the leaf as absent; the safe descent rejects it
    # (``ELOOP``) and returns False (ambiguous -> keep visible).
    root = tmp_path / "legacy"
    root.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    (root / "sub").symlink_to(empty)
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("sub/secret.json")) is False


@pytest.mark.unit
def test_legacy_path_confidently_absent_true_for_missing_parent_subtree(tmp_path: Path) -> None:
    # The whole parent subtree is gone (a whole-directory deletion): the leaf is absent
    # too, and the pass forwards it file-by-file. Confidently absent.
    root = tmp_path / "legacy"
    root.mkdir()
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("sub/secret.json")) is True


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_for_non_directory_parent(tmp_path: Path) -> None:
    # A parent component replaced with a regular file (``ENOTDIR``): the path cannot be
    # walked as real directories, so the absence is ambiguous -> keep visible.
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "sub").write_text("not a dir")
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("sub/secret.json")) is False


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_when_leaf_is_symlink(tmp_path: Path) -> None:
    # A symlink leaf (even dangling) is ``lstat``ed as itself: present, not a deletion.
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "secret.json").symlink_to(tmp_path / "elsewhere")
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("secret.json")) is False


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_when_root_missing(tmp_path: Path) -> None:
    # The ``root`` itself is unopenable (absent): ambiguous -> not a confident deletion.
    assert overlay_copy_mod._legacy_path_confidently_absent(tmp_path / "nope", Path("x")) is False


@pytest.mark.unit
def test_legacy_path_confidently_absent_false_when_leaf_stat_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-``ENOENT`` leaf ``stat`` failure (e.g. a racing permission error) is ambiguous,
    # not a proven deletion -> fail safe to False.
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "secret.json").write_text("x")

    def _raise_eacces(*args: object, **kwargs: object) -> os.stat_result:
        raise PermissionError("denied")

    monkeypatch.setattr(overlay_copy_mod.os, "stat", _raise_eacces)
    assert overlay_copy_mod._legacy_path_confidently_absent(root, Path("secret.json")) is False
