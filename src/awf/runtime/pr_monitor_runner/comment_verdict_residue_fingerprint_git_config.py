"""Item-start local Git config snapshot/restore and trusted HEAD probes.

Extracted from ``comment_verdict_residue_fingerprint`` to stay under the
first-party line budget. Public names are re-exported from that module for
callers and monkeypatch surfaces.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import os
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
    _module_git_dirs_under,
    _nested_worktree_roots_with_git_markers,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_pinned_worktree_command,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


# Item-start local Git config snapshots keyed by resolved worktree path so
# protocol-retry rollback can restore config-only mutations
# (PRRT_kwDOSJAM6s6e0Xdl) without threading the blob through every call site.
_ITEM_START_LOCAL_GIT_CONFIGS: dict[str, dict[str, dict[str, str]]] = {}
# Outer worktree ``.git`` gitfile text at item start (linked worktrees only).
# Missing key / absent when the marker was a directory or remember failed.
_ITEM_START_GIT_LINKAGE: dict[str, str] = {}
# Nested checkout ``.git`` gitfile texts: worktree_key -> {nested_root: text}.
_ITEM_START_NESTED_GIT_LINKAGES: dict[str, dict[str, str]] = {}

_LOCAL_GIT_CONFIG_NAMES: tuple[str, ...] = ("config", "config.worktree")
_GITDIR_PREFIX = "gitdir:"
# Fingerprint-only keys folded into git-meta (never restored as config).
_HEAD_IDENTITY_NAME = "HEAD"
_HEAD_TIP_IDENTITY_NAME = "HEAD.tip"
_REF_PREFIX = "ref:"
# Packed-refs in bare mirrors routinely exceed the local-config size cap; tip
# lookup streams under its own byte/deadline bounds (PRRT_kwDOSJAM6s6fHJIm).
_PACKED_REFS_SCAN_MAX_BYTES = 64 * 1024 * 1024
_PACKED_REFS_SCAN_CHUNK_BYTES = 64 * 1024
_PACKED_REFS_SCAN_BUDGET_SECONDS = 10.0
_PACKED_REFS_SCAN_MAX_LINE_BYTES = 64 * 1024


def _packed_refs_tip_from_line(raw_line: bytes, ref_name: str) -> str | None:
    """Return tip text when ``raw_line`` names ``ref_name``, else ``None``."""
    line = raw_line.decode("utf-8", errors="surrogateescape").strip()
    if not line or line.startswith("#") or line.startswith("^"):
        return None
    parts = line.split()
    if len(parts) != 2:
        return None
    tip, name = parts
    if name != ref_name:
        return None
    return f"{tip}\n"


def _read_packed_refs_tip_for_name(packed_path: Path, ref_name: str) -> str | None:
    """Stream ``packed-refs`` for ``ref_name`` under a dedicated scan budget.

    Returns the tip line text, ``""`` when the file is missing or the ref is
    absent, and ``None`` to fail closed. Unlike ``_read_git_dir_config_text``,
    this does not apply ``_GIT_DIR_CONFIG_MAX_BYTES`` — mirrors with thousands
    of packed refs are healthy above that cap (PRRT_kwDOSJAM6s6fHJIm). When
    invoked under ``_residue_git_config_snapshot_budget``, also fail closed on
    the shared snapshot wall deadline so nested packed-refs scans cannot stall
    past the aggregate fingerprint bound (PRRT_kwDOSJAM6s6fHliF).
    """
    from awf.node.git_manager_ownership import (
        _GIT_DIR_CONFIG_OPEN_FLAGS,
        _git_config_snapshot_budget_past_deadline,
    )

    def _stable_regular(st_open: os.stat_result) -> bool:
        try:
            st_after = os.fstat(fd)
        except OSError:
            return False
        return (
            stat.S_ISREG(st_after.st_mode)
            and st_after.st_size == st_open.st_size
            and st_after.st_ino == st_open.st_ino
            and st_after.st_dev == st_open.st_dev
            and st_after.st_mtime_ns == st_open.st_mtime_ns
            and st_after.st_ctime_ns == st_open.st_ctime_ns
        )

    try:
        fd = os.open(packed_path, _GIT_DIR_CONFIG_OPEN_FLAGS)
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size < 0 or st.st_size > _PACKED_REFS_SCAN_MAX_BYTES:
            return None
        deadline = time.monotonic() + _PACKED_REFS_SCAN_BUDGET_SECONDS
        scanned = 0
        pending = b""
        while scanned < st.st_size:
            if time.monotonic() >= deadline:
                return None
            if _git_config_snapshot_budget_past_deadline():
                return None
            to_read = min(_PACKED_REFS_SCAN_CHUNK_BYTES, st.st_size - scanned)
            try:
                chunk = os.read(fd, to_read)
            except OSError:
                return None
            if not chunk:
                break
            scanned += len(chunk)
            pending += chunk
            while True:
                nl = pending.find(b"\n")
                if nl < 0:
                    if len(pending) > _PACKED_REFS_SCAN_MAX_LINE_BYTES:
                        return None
                    break
                raw_line = pending[:nl]
                pending = pending[nl + 1 :]
                tip = _packed_refs_tip_from_line(raw_line, ref_name)
                if tip is not None:
                    return tip if _stable_regular(st) else None
        if pending:
            if len(pending) > _PACKED_REFS_SCAN_MAX_LINE_BYTES:
                return None
            tip = _packed_refs_tip_from_line(pending, ref_name)
            if tip is not None:
                return tip if _stable_regular(st) else None
        if not _stable_regular(st):
            return None
        return ""
    finally:
        os.close(fd)


def _snapshot_git_dir_head_identity_fields(git_dir: Path) -> dict[str, str] | None:
    """Return trusted HEAD / tip fields for git-meta, or ``None`` to fail closed.

    Outer porcelain stays clean when a nested repo under a tracked path advances
    its symbolic-ref tip while ``config`` / ``config.worktree`` are unchanged
    (PRRT_kwDOSJAM6s6fG5gn). Include HEAD text and the resolved tip so before /
    after fingerprints cannot collide on tip-only mutations.
    """
    from awf.node.git_manager_ownership import _read_git_dir_config_text

    head_path = git_dir / "HEAD"
    try:
        mode = head_path.lstat().st_mode
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return None
    head_text = _read_git_dir_config_text(head_path)
    if head_text is None:
        return None

    body = head_text.lstrip("\ufeff").strip()
    if body.startswith(_REF_PREFIX):
        ref_name = body[len(_REF_PREFIX) :].strip()
        if (
            not ref_name
            or not ref_name.startswith("refs/")
            or ref_name.endswith("/")
            or "\\" in ref_name
            or ".." in Path(ref_name).parts
        ):
            return None
        loose_path = git_dir.joinpath(*Path(ref_name).parts)
        try:
            loose_mode = loose_path.lstat().st_mode
        except FileNotFoundError:
            packed_tip = _read_packed_refs_tip_for_name(git_dir / "packed-refs", ref_name)
            if packed_tip is None:
                return None
            return {
                _HEAD_IDENTITY_NAME: head_text,
                _HEAD_TIP_IDENTITY_NAME: packed_tip,
            }
        except OSError:
            return None
        if stat.S_ISLNK(loose_mode) or not stat.S_ISREG(loose_mode):
            return None
        tip_text = _read_git_dir_config_text(loose_path)
        if tip_text is None:
            return None
        return {
            _HEAD_IDENTITY_NAME: head_text,
            _HEAD_TIP_IDENTITY_NAME: tip_text,
        }

    # Detached HEAD: the file contents are the tip identity.
    return {
        _HEAD_IDENTITY_NAME: head_text,
        _HEAD_TIP_IDENTITY_NAME: head_text,
    }


def _snapshot_outer_gitfile_text(worktree_path: Path) -> tuple[bool, str | None]:
    """Return ``(ok, gitfile_text_or_none)`` for the outer worktree ``.git`` marker.

    ``None`` text means a directory marker (or absent) — nothing to restore.
    ``ok=False`` means a regular/symlink/unreadable marker could not be trusted.
    """
    from awf.node.git_manager_ownership import _read_git_dir_config_text

    marker = worktree_path / ".git"
    try:
        mode = marker.lstat().st_mode
    except FileNotFoundError:
        return True, None
    except OSError:
        return False, None
    if stat.S_ISDIR(mode):
        return True, None
    if not stat.S_ISREG(mode):
        return False, None
    text = _read_git_dir_config_text(marker)
    if text is None:
        return False, None
    if not text.lstrip("\ufeff").startswith(_GITDIR_PREFIX):
        return False, None
    return True, text


def _resolve_gitfile_target(worktree_path: Path, gitfile_text: str) -> Path | None:
    """Resolve a ``gitdir:`` target from snapshotted gitfile text."""
    body = gitfile_text.lstrip("\ufeff").strip()
    if not body.startswith(_GITDIR_PREFIX):
        return None
    raw = body[len(_GITDIR_PREFIX) :].strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = worktree_path / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return None


def _gitfile_target_path_without_follow(worktree_path: Path, gitfile_text: str) -> Path | None:
    """Return the remembered ``gitdir:`` path without following live symlinks.

    ``Path.resolve()`` would walk a post-probe symlink swap into a foreign
    workspace (PRRT_kwDOSJAM6s6fH7-s). Keep the absolute/lexical form so
    ``O_NOFOLLOW`` open can refuse the swapped component.
    """
    body = gitfile_text.lstrip("\ufeff").strip()
    if not body.startswith(_GITDIR_PREFIX):
        return None
    raw = body[len(_GITDIR_PREFIX) :].strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        try:
            git_dir = worktree_path.resolve() / git_dir
        except OSError:
            return None
    return Path(os.path.normpath(git_dir))


def item_start_has_gitfile_linkage(worktree_path: Path) -> bool:
    """True when remember stored an outer ``gitdir:`` marker for this worktree."""
    if not worktree_path.exists():
        return False
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    return key in _ITEM_START_GIT_LINKAGE


@contextlib.contextmanager
def hold_item_start_pinned_git_dir(worktree_path: Path) -> Iterator[Path | None]:
    """Hold an ``O_NOFOLLOW`` open of the remembered linked git-dir.

    Yields the opened inode's pathname for ``--git-dir`` while the descriptor
    remains open, or ``None`` when there is no linkage / open fails closed
    (PRRT_kwDOSJAM6s6fH7-s).
    """
    if not worktree_path.exists():
        yield None
        return
    try:
        key = str(worktree_path.resolve())
    except OSError:
        yield None
        return
    text = _ITEM_START_GIT_LINKAGE.get(key)
    if text is None:
        yield None
        return
    target = _gitfile_target_path_without_follow(worktree_path, text)
    if target is None:
        yield None
        return
    with _open_snapshotted_git_dir_for_restore(
        target,
        outer_worktree_path=worktree_path,
    ) as git_dir_fd:
        if git_dir_fd is None:
            yield None
            return
        try:
            pinned = Path(f"/proc/self/fd/{git_dir_fd}").readlink()
        except OSError:
            yield None
            return
        yield pinned


def item_start_pinned_git_dir(worktree_path: Path) -> Path | None:
    """Return the remembered item-start linked git-dir for pinned rollback commands.

    Refuses symlink-swapped targets via ``O_NOFOLLOW`` (PRRT_kwDOSJAM6s6fH7-s).
    """
    with hold_item_start_pinned_git_dir(worktree_path) as pinned:
        return pinned


def _clear_item_start_git_caches(key: str) -> None:
    _ITEM_START_LOCAL_GIT_CONFIGS.pop(key, None)
    _ITEM_START_GIT_LINKAGE.pop(key, None)
    _ITEM_START_NESTED_GIT_LINKAGES.pop(key, None)


def _snapshot_nested_gitfile_linkages(
    nested_roots: tuple[Path, ...],
) -> dict[str, str] | None:
    """Return ``{nested_root: gitfile_text}`` for nested gitfiles; ``None`` fail-closed."""
    out: dict[str, str] = {}
    for nested_root in nested_roots:
        ok, text = _snapshot_outer_gitfile_text(nested_root)
        if not ok:
            return None
        if text is None:
            continue
        try:
            out[str(nested_root.resolve())] = text
        except OSError:
            return None
    return out


def _snapshot_worktree_local_git_configs(
    worktree_path: Path,
    *,
    nested_linkages_out: dict[str, str] | None = None,
) -> dict[str, dict[str, str]] | None:
    """Return ``{resolved_git_dir: {config_name: text}}`` or ``None`` to fail closed.

    When ``nested_linkages_out`` is provided, fill it with nested checkout
    ``.git`` gitfile texts discovered during the same walk
    (PRRT_kwDOSJAM6s6e65b_).
    """
    from awf.node.git_manager_ownership import (
        _nested_repository_git_dirs_for_include_scan,
        _residue_git_config_snapshot_budget,
        _snapshot_git_dir_local_configs,
    )
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
        _approved_git_metadata_roots,
    )

    roots = _approved_git_metadata_roots(worktree_path)
    if not roots:
        return None
    git_dirs = _nested_repository_git_dirs_for_include_scan(
        worktree_path,
        containment_roots=roots,
    )
    if git_dirs is None:
        return None
    # Formal submodule stores under ``modules/`` plus any checked-out nested
    # repositories with their own ``.git`` markers (PRRT_kwDOSJAM6s6e4egX).
    # One shared directory-enum budget covers module walks and nested checkout
    # discovery so the two scans cannot independently spend 100k entries
    # (PRRT_kwDOSJAM6s6e5zYG).
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _residue_directory_enum_budget,
    )

    extra_dirs: list[Path] = []
    with _residue_directory_enum_budget():
        for outer in git_dirs:
            modules = _module_git_dirs_under(outer, roots=roots)
            if modules is None:
                return None
            extra_dirs.extend(modules)
        nested_roots = _nested_worktree_roots_with_git_markers(worktree_path)
        if nested_roots is None:
            return None
        if nested_linkages_out is not None:
            linkages = _snapshot_nested_gitfile_linkages(nested_roots)
            if linkages is None:
                return None
            nested_linkages_out.update(linkages)
        for nested_root in nested_roots:
            nested_dirs = _nested_repository_git_dirs_for_include_scan(
                nested_root,
                containment_roots=roots,
            )
            if nested_dirs is None:
                return None
            extra_dirs.extend(nested_dirs)
            for nested_git_dir in nested_dirs:
                modules = _module_git_dirs_under(nested_git_dir, roots=roots)
                if modules is None:
                    return None
                extra_dirs.extend(modules)

    with _residue_git_config_snapshot_budget():
        out: dict[str, dict[str, str]] = {}
        for git_dir in (*git_dirs, *extra_dirs):
            snap = _snapshot_git_dir_local_configs(git_dir)
            if snap is None:
                return None
            head_fields = _snapshot_git_dir_head_identity_fields(git_dir)
            if head_fields is None:
                return None
            try:
                key = str(git_dir.resolve())
            except OSError:
                return None
            merged = dict(snap)
            merged.update(head_fields)
            out[key] = merged
    return out


def _hash_local_git_config_snapshot(snapshot: dict[str, dict[str, str]]) -> str:
    """Digest local Git config snapshots, including configless git-dir keys.

    Nested markers such as ``src/.git/{HEAD,objects/,refs/}`` can be functional
    with an empty config map. Emitting the directory name only inside the
    per-config loop made ``{git_dir: {}}`` hash identically to key absence, so
    ``git-meta:`` stayed stable while porcelain remained clean
    (PRRT_kwDOSJAM6s6fGqDa). Always hash each discovered git-dir identity, then
    its configs, then an end-of-directory sentinel.

    Pre-existing nested repositories under tracked paths can also advance HEAD
    (or only the symbolic-ref tip) without touching config text or outer
    porcelain (PRRT_kwDOSJAM6s6fG5gn). Snapshot fields ``HEAD`` / ``HEAD.tip``
    are included in the same digest so tip-only mutations cannot collide.
    """
    digest = hashlib.sha256()
    for git_dir in sorted(snapshot):
        configs = snapshot[git_dir]
        digest.update(git_dir.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        for name in sorted(configs):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(configs[name].encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
        digest.update(b"\0")
    return digest.hexdigest()


def remember_item_start_local_git_configs(worktree_path: Path) -> bool:
    """Snapshot worktree-local Git configs for later protocol-retry restore."""
    if not worktree_path.exists():
        return True
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    linkage_ok, linkage_text = _snapshot_outer_gitfile_text(worktree_path)
    if not linkage_ok:
        _clear_item_start_git_caches(key)
        return False
    nested_linkages: dict[str, str] = {}
    # Resolve via host module so tests can monkeypatch
    # ``comment_verdict_residue_fingerprint._snapshot_worktree_local_git_configs``.
    from awf.runtime.pr_monitor_runner import (
        comment_verdict_residue_fingerprint as _host,
    )

    snapshot = _host._snapshot_worktree_local_git_configs(
        worktree_path,
        nested_linkages_out=nested_linkages,
    )
    if snapshot is None:
        # Drop any prior entry so a later rollback cannot restore a stale blob
        # from an earlier item on a reused worktree path (PRRT_kwDOSJAM6s6e0xSO).
        _clear_item_start_git_caches(key)
        return False
    _ITEM_START_LOCAL_GIT_CONFIGS[key] = snapshot
    _ITEM_START_NESTED_GIT_LINKAGES[key] = nested_linkages
    if linkage_text is None:
        _ITEM_START_GIT_LINKAGE.pop(key, None)
    else:
        _ITEM_START_GIT_LINKAGE[key] = linkage_text
    return True


def _write_local_git_config_file(path: Path, text: str) -> bool:
    """Replace a local config file via a fresh inode (never open the destination).

    Opening the destination with ``O_TRUNC`` truncates hard-linked targets and
    blocks forever on a reader-less FIFO before any post-open ``fstat`` guard can
    refuse a non-regular file. Write a sibling temp file with ``O_EXCL`` and
    atomically replace the directory entry instead (PRRT_kwDOSJAM6s6e2x5c).

    After the write fd is closed, rename still keys off the temp pathname. A
    surviving agent can swap that name for a symlink or other content before
    ``replace``, so restore would otherwise report success while installing
    untrusted config or ``.git`` linkage (PRRT_kwDOSJAM6s6e3DXZ). Re-open the
    temp with ``O_NOFOLLOW``, require the same ``(st_dev, st_ino)`` we wrote,
    require the exact restore bytes, then re-verify the destination the same
    way after replace — fail closed on any mismatch.
    """
    encoded = text.encode("utf-8", errors="surrogateescape")
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    verify_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    def _fd_matches_trusted_bytes(fd: int, expected_dev: int, expected_ino: int) -> bool:
        try:
            st = os.fstat(fd)
        except OSError:  # pragma: no cover - descriptor revoked mid-verify
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_dev != expected_dev or st.st_ino != expected_ino:
            return False
        if st.st_size != len(encoded):
            return False
        try:
            got = os.read(fd, len(encoded) + 1)
        except OSError:  # pragma: no cover - descriptor revoked mid-verify
            return False
        return got == encoded

    def _path_matches_trusted_bytes(candidate: Path, expected_dev: int, expected_ino: int) -> bool:
        try:
            verify_fd = os.open(candidate, verify_flags)
        except OSError:
            return False
        try:
            return _fd_matches_trusted_bytes(verify_fd, expected_dev, expected_ino)
        finally:
            os.close(verify_fd)

    try:
        fd = os.open(tmp_path, flags, 0o644)
    except OSError:
        return False
    succeeded = False
    try:
        try:
            try:
                st = os.fstat(fd)
            except OSError:
                return False
            if not stat.S_ISREG(st.st_mode):  # pragma: no cover - O_EXCL creates a regular file
                return False
            remaining = memoryview(encoded)
            while remaining:
                try:
                    written = os.write(fd, remaining)
                except OSError:
                    return False
                if written <= 0:  # pragma: no cover - defensive
                    return False
                remaining = remaining[written:]
            trusted_dev = st.st_dev
            trusted_ino = st.st_ino
        finally:
            os.close(fd)
        if not _path_matches_trusted_bytes(tmp_path, trusted_dev, trusted_ino):
            return False
        try:
            tmp_path.replace(path)
        except OSError:
            return False
        # Post-replace: destination must still be our inode with trusted bytes.
        # A swap that wins between the pre-check and replace changes identity.
        try:
            dest_st = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISREG(dest_st.st_mode):
            return False
        if dest_st.st_dev != trusted_dev or dest_st.st_ino != trusted_ino:
            return False
        if not _path_matches_trusted_bytes(path, trusted_dev, trusted_ino):
            return False
        succeeded = True
        return True
    finally:
        if not succeeded:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _write_local_git_config_file_at(dir_fd: int, name: str, text: str) -> bool:
    """Replace a directory-relative config file via a fresh inode (never open the destination).

    Same safety model as ``_write_local_git_config_file``, but every create/replace/verify
    step is relative to a pinned parent ``dir_fd`` so agent-replaceable pathnames cannot
    redirect restore outside the outer checkout (PRRT_kwDOSJAM6s6e9Z2x).
    """
    encoded = text.encode("utf-8", errors="surrogateescape")
    tmp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    verify_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    def _fd_matches_trusted_bytes(fd: int, expected_dev: int, expected_ino: int) -> bool:
        try:
            st = os.fstat(fd)
        except OSError:  # pragma: no cover - descriptor revoked mid-verify
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        if st.st_dev != expected_dev or st.st_ino != expected_ino:
            return False
        if st.st_size != len(encoded):
            return False
        try:
            got = os.read(fd, len(encoded) + 1)
        except OSError:  # pragma: no cover - descriptor revoked mid-verify
            return False
        return got == encoded

    def _entry_matches_trusted_bytes(
        entry_name: str,
        expected_dev: int,
        expected_ino: int,
    ) -> bool:
        try:
            verify_fd = os.open(entry_name, verify_flags, dir_fd=dir_fd)
        except OSError:
            return False
        try:
            return _fd_matches_trusted_bytes(verify_fd, expected_dev, expected_ino)
        finally:
            os.close(verify_fd)

    try:
        fd = os.open(tmp_name, flags, 0o644, dir_fd=dir_fd)
    except OSError:
        return False
    succeeded = False
    try:
        try:
            try:
                st = os.fstat(fd)
            except OSError:
                return False
            if not stat.S_ISREG(st.st_mode):  # pragma: no cover - O_EXCL creates a regular file
                return False
            remaining = memoryview(encoded)
            while remaining:
                try:
                    written = os.write(fd, remaining)
                except OSError:
                    return False
                if written <= 0:  # pragma: no cover - defensive
                    return False
                remaining = remaining[written:]
            trusted_dev = st.st_dev
            trusted_ino = st.st_ino
        finally:
            os.close(fd)
        if not _entry_matches_trusted_bytes(tmp_name, trusted_dev, trusted_ino):
            return False
        try:
            os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            return False
        try:
            dest_st = os.lstat(name, dir_fd=dir_fd)
        except OSError:
            return False
        if not stat.S_ISREG(dest_st.st_mode):
            return False
        if dest_st.st_dev != trusted_dev or dest_st.st_ino != trusted_ino:
            return False
        if not _entry_matches_trusted_bytes(name, trusted_dev, trusted_ino):
            return False
        succeeded = True
        return True
    finally:
        if not succeeded:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)


def _restore_worktree_git_linkage(worktree_path: Path, gitfile_text: str) -> bool:
    """Rewrite the outer worktree ``.git`` gitfile to the item-start text."""
    return _write_local_git_config_file(worktree_path / ".git", gitfile_text)


def _restore_nested_git_linkages(
    linkages: Mapping[str, str],
    *,
    outer_worktree_path: Path,
) -> bool:
    """Rewrite nested checkout ``.git`` gitfiles to their item-start texts."""
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _open_worktree_directory_path,
    )

    for nested_root, gitfile_text in linkages.items():
        with _open_worktree_directory_path(
            Path(nested_root),
            outer_worktree_path=outer_worktree_path,
        ) as nested_fd:
            if nested_fd is None:
                return False
            if not _write_local_git_config_file_at(nested_fd, ".git", gitfile_text):
                return False
    return True


@contextlib.contextmanager
def _open_snapshotted_git_dir_for_restore(
    git_dir: Path,
    *,
    outer_worktree_path: Path,
) -> Iterator[int | None]:
    """Open a snapshotted git-dir via approved-root containment and ``O_NOFOLLOW``.

    Absolute snapshot keys must not be re-opened by pathname: an agent can replace
    a path component with a symlink to another workspace, and pathname
    ``O_NOFOLLOW`` only protects the final temp entry
    (PRRT_kwDOSJAM6s6fC3mj). Lexical ``relative_to`` against approved roots
    (checkout + linked ``mirrors/``) plus no-follow descent fails closed on
    any swapped component.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _WORKTREE_DIRECTORY_OPEN_FLAGS,
        _open_worktree_directory,
    )
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
        _approved_git_metadata_roots,
    )

    if not git_dir.is_absolute():
        yield None
        return
    for root in _approved_git_metadata_roots(outer_worktree_path):
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        try:
            relative = git_dir.relative_to(root_resolved)
        except ValueError:
            continue
        if not relative.parts:
            try:
                dir_fd = os.open(root, _WORKTREE_DIRECTORY_OPEN_FLAGS)
            except OSError:
                yield None
                return
            try:
                if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                    yield None
                    return
                yield dir_fd
            finally:
                os.close(dir_fd)
            return
        nested_cm = _open_worktree_directory(root, relative.as_posix())
        try:
            dir_fd = nested_cm.__enter__()
        except OSError:
            yield None
            return
        try:
            yield dir_fd
        finally:
            nested_cm.__exit__(None, None, None)
        return
    yield None


def _restore_worktree_local_git_configs(
    snapshot: dict[str, dict[str, str]],
    *,
    outer_worktree_path: Path,
) -> bool:
    """Rewrite snapshotted local configs and remove agent-created extras."""
    for git_dir_key, configs in snapshot.items():
        with _open_snapshotted_git_dir_for_restore(
            Path(git_dir_key),
            outer_worktree_path=outer_worktree_path,
        ) as git_dir_fd:
            if git_dir_fd is None:
                return False
            for name in _LOCAL_GIT_CONFIG_NAMES:
                if name in configs:
                    if not _write_local_git_config_file_at(git_dir_fd, name, configs[name]):
                        return False
                    continue
                try:
                    mode = os.lstat(name, dir_fd=git_dir_fd).st_mode
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
                    try:
                        os.unlink(name, dir_fd=git_dir_fd)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        return False
                else:
                    return False
    return True


def restore_item_start_local_git_configs(worktree_path: Path) -> bool:
    """Restore the remembered item-start local Git config snapshot, if any."""
    if not worktree_path.exists():
        return True
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    linkage_text = _ITEM_START_GIT_LINKAGE.get(key)
    if linkage_text is not None and not _restore_worktree_git_linkage(worktree_path, linkage_text):
        return False
    nested_linkages = _ITEM_START_NESTED_GIT_LINKAGES.get(key)
    if nested_linkages and not _restore_nested_git_linkages(
        nested_linkages,
        outer_worktree_path=worktree_path,
    ):
        return False
    snapshot = _ITEM_START_LOCAL_GIT_CONFIGS.get(key)
    if snapshot is None:
        return True
    return _restore_worktree_local_git_configs(snapshot, outer_worktree_path=worktree_path)


def item_start_has_local_git_config_snapshot(worktree_path: Path) -> bool:
    """True when remember_item_start_local_git_configs stored a snapshot for this path."""
    if not worktree_path.exists():
        return False
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    return key in _ITEM_START_LOCAL_GIT_CONFIGS


def item_start_snapshot_covers_outer_git_dir(worktree_path: Path) -> bool:
    """True when the remembered snapshot includes configs for the outer git-dir.

    Empty fixture worktrees can ``remember`` an empty map; those must not block
    falling back to ``_rev_parse_head`` mocks. A real snapshot that covers the
    outer git-dir is required before we refuse live Git (PRRT_kwDOSJAM6s6e30Rp).
    """
    if not item_start_has_local_git_config_snapshot(worktree_path):
        return False
    live_git_dir = _item_start_outer_git_dir(worktree_path)
    if live_git_dir is None:
        return False
    try:
        key = str(worktree_path.resolve())
        live_key = str(live_git_dir.resolve())
    except OSError:
        return False
    snapshot = _ITEM_START_LOCAL_GIT_CONFIGS.get(key)
    if not snapshot:
        return False
    return live_key in snapshot


def _item_start_outer_git_dir(worktree_path: Path) -> Path | None:
    """Return the outer repository git-dir to probe (pinned link or ``.git`` dir)."""
    pinned = item_start_pinned_git_dir(worktree_path)
    if pinned is not None:
        return pinned
    marker = worktree_path / ".git"
    try:
        mode = marker.lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISDIR(mode):
        return None
    try:
        return marker.resolve()
    except OSError:
        return None


def _write_trusted_local_configs(
    staging: Path,
    configs: dict[str, str],
    *,
    original_git_dir: Path,
) -> bool:
    """Write remembered local config texts into ``staging``."""
    from awf.node.git_manager_ownership import _rewrite_relative_core_worktree_for_snapshot

    main = configs.get("config", "[core]\n\trepositoryformatversion = 0\n")
    rewritten = _rewrite_relative_core_worktree_for_snapshot(main, original_git_dir)
    if rewritten is None:
        return False
    try:
        (staging / "config").write_bytes(rewritten.encode("utf-8", errors="surrogateescape"))
    except OSError:
        return False
    worktree_config = configs.get("config.worktree")
    if worktree_config is None:
        return True
    rewritten_wt = _rewrite_relative_core_worktree_for_snapshot(worktree_config, original_git_dir)
    if rewritten_wt is None:
        return False
    try:
        (staging / "config.worktree").write_bytes(
            rewritten_wt.encode("utf-8", errors="surrogateescape")
        )
    except OSError:
        return False
    return True


def _materialize_trusted_git_dir_from_live(
    *,
    live_git_dir: Path,
    configs: dict[str, str],
    staging: Path,
    require_head: bool = True,
) -> bool:
    """Populate ``staging`` with trusted configs and HEAD-resolution stores.

    Symlinked ``objects`` / ``refs`` / ``packed-refs`` would resolve into a
    foreign workspace and poison the trusted HEAD probe
    (PRRT_kwDOSJAM6s6fFF47). Reject store symlinks, privately copy
    non-symlink ``refs`` / ``packed-refs`` leaves, and stage an empty private
    ``objects`` directory: ``rev-parse HEAD`` only needs ref data, and
    copying live packs would impose nested object-store leaf/aggregate caps
    on ordinary repositories (PRRT_kwDOSJAM6s6fGb8b).
    """
    from awf.node import git_manager_ownership as ownership
    from awf.node.git_manager_ownership import _read_git_dir_config_text

    if not _write_trusted_local_configs(staging, configs, original_git_dir=live_git_dir):
        return False
    head_text = _read_git_dir_config_text(live_git_dir / "HEAD")
    if head_text is None:
        if require_head:
            return False
    else:
        try:
            (staging / "HEAD").write_bytes(head_text.encode("utf-8", errors="surrogateescape"))
        except OSError:
            return False

    git_dir_fd = ownership._open_git_dir_directory_fd(live_git_dir)
    if git_dir_fd is None:
        return False
    held_fds: list[int] = []
    try:
        # Outer trusted probes require a real objects/refs directory; missing or
        # symlinked stores fail closed (unlike nested probes that tolerate absent
        # stores on bare stubs). Do not copy live object packs — HEAD resolve
        # uses refs only (PRRT_kwDOSJAM6s6fGb8b).
        try:
            objects_st = os.stat("objects", dir_fd=git_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if stat.S_ISLNK(objects_st.st_mode) or not stat.S_ISDIR(objects_st.st_mode):
            return False
        try:
            (staging / "objects").mkdir()
        except OSError:
            return False

        try:
            refs_st = os.stat("refs", dir_fd=git_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if stat.S_ISLNK(refs_st.st_mode) or not stat.S_ISDIR(refs_st.st_mode):
            return False
        refs_ok, refs_fds = ownership._symlink_nested_probe_refs_store_via_fd(git_dir_fd, staging)
        held_fds.extend(refs_fds)
        if not refs_ok:
            return False

        return ownership._symlink_git_dir_child_via_fd(
            git_dir_fd,
            "packed-refs",
            staging / "packed-refs",
            held_fds,
            expect_directory=False,
        )
    finally:
        for held in held_fds:
            with contextlib.suppress(OSError):
                os.close(held)
        with contextlib.suppress(OSError):
            os.close(git_dir_fd)


@contextlib.contextmanager
def item_start_trusted_head_probe_git_dir(worktree_path: Path) -> Iterator[Path | None]:
    """Yield a private git-dir that uses remembered item-start local configs.

    Attempt 0 can inject ``include.path`` → FIFO into live config before the
    correction-start HEAD probe (PRRT_kwDOSJAM6s6e30Rp). Yields ``None`` when
    no snapshot exists or materialization fails closed.
    """
    if not item_start_has_local_git_config_snapshot(worktree_path):
        yield None
        return
    try:
        key = str(worktree_path.resolve())
    except OSError:
        yield None
        return
    snapshot = _ITEM_START_LOCAL_GIT_CONFIGS.get(key)
    live_git_dir = _item_start_outer_git_dir(worktree_path)
    if snapshot is None or live_git_dir is None:
        yield None
        return
    try:
        live_key = str(live_git_dir.resolve())
    except OSError:
        yield None
        return
    configs = snapshot.get(live_key)
    if configs is None:
        yield None
        return

    from awf.node.git_manager_ownership import _read_git_dir_config_text

    staging_root = Path(tempfile.mkdtemp(prefix="awf-item-start-head-"))
    try:
        commondir_text = _read_git_dir_config_text(live_git_dir / "commondir")
        if commondir_text is not None:
            raw = commondir_text.strip()
            if not raw:
                yield None
                return
            common_path = Path(raw)
            if not common_path.is_absolute():
                common_path = live_git_dir / common_path
            try:
                common_path = common_path.resolve()
            except OSError:
                yield None
                return
            common_configs = snapshot.get(str(common_path))
            if common_configs is None:
                yield None
                return
            staging_common = staging_root / "common"
            staging = staging_root / "worktree"
            try:
                staging_common.mkdir()
                staging.mkdir()
            except OSError:
                yield None
                return
            if not _materialize_trusted_git_dir_from_live(
                live_git_dir=common_path,
                configs=common_configs,
                staging=staging_common,
                require_head=False,
            ):
                yield None
                return
            if not _write_trusted_local_configs(staging, configs, original_git_dir=live_git_dir):
                yield None
                return
            head_text = _read_git_dir_config_text(live_git_dir / "HEAD")
            if head_text is None:
                yield None
                return
            try:
                (staging / "HEAD").write_bytes(head_text.encode("utf-8", errors="surrogateescape"))
                (staging / "commondir").write_text(
                    f"{staging_common.resolve()}\n", encoding="utf-8"
                )
            except OSError:
                yield None
                return
            yield staging
            return

        staging = staging_root / "git"
        try:
            staging.mkdir()
        except OSError:
            yield None
            return
        if not _materialize_trusted_git_dir_from_live(
            live_git_dir=live_git_dir,
            configs=configs,
            staging=staging,
            require_head=True,
        ):
            yield None
            return
        yield staging
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


async def rev_parse_head_via_item_start_trust(
    runner: PullRequestMonitorRunner,
    worktree_path: Path,
) -> str | None:
    """Resolve HEAD through remembered item-start configs with a finite timeout.

    Returns ``None`` when the trusted probe is unavailable or Git fails/times out.
    Callers that still have no snapshot should fall back to live ``_rev_parse_head``.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    with item_start_trusted_head_probe_git_dir(worktree_path) as probe_git_dir:
        if probe_git_dir is None:
            return None
        result = await runner._deps.runner.run(
            git_pinned_worktree_command(probe_git_dir, worktree_path, "rev-parse", "HEAD"),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    if not result.ok:
        return None
    value = result.stdout.strip()
    return value or None


async def read_protocol_attempt_start_head(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    rev_parse_head: Callable[..., Awaitable[str | None]] | None,
) -> str | None:
    """Read attempt/correction-start HEAD without hanging on poisoned live config.

    When an item-start config snapshot covers the outer git-dir, probe only
    through that trusted private git-dir (PRRT_kwDOSJAM6s6e30Rp). Otherwise fall
    back to the runner's ``_rev_parse_head``, passing a timeout when the
    callable accepts it.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )

    if item_start_snapshot_covers_outer_git_dir(worktree_path):
        # Covered snapshot: never fall back to live Git (include.path → FIFO hang).
        return await rev_parse_head_via_item_start_trust(runner, worktree_path)
    if not callable(rev_parse_head):
        return None
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(rev_parse_head).parameters
    except (TypeError, ValueError):
        parameters = None
    if parameters is not None and "timeout_seconds" in parameters:
        kwargs["timeout_seconds"] = _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    return await rev_parse_head(worktree_path, **kwargs)
