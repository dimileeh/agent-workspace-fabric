"""Correction-attempt residue fingerprint API for verdict protocol retries.

Kept separate so ``comment_verdict_residue`` stays under the first-party line budget.
Hashing / nested-probe helpers remain in ``comment_verdict_residue``; this module owns
porcelain decode and the correction fingerprint / mutation predicates.
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
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
    _module_git_dirs_under,
    _nested_worktree_roots_with_git_markers,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_pinned_worktree_command,
    git_worktree_command,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

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


def _decode_porcelain_status_stdout(
    *,
    stdout: str,
    stdout_bytes: bytes | None,
) -> tuple[str, bool]:
    """Return decoded porcelain and whether NUL-delimited ``-z`` records are present."""
    if stdout_bytes is not None:
        return stdout_bytes.decode("utf-8", errors="surrogateescape"), True
    if "\0" in stdout:
        return stdout, True
    return stdout, False


def _format_porcelain_z_line(status: str, path: str, original_path: str | None) -> str:
    if original_path:
        return f"{status} {original_path} -> {path}"
    return f"{status} {path}"


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


def item_start_pinned_git_dir(worktree_path: Path) -> Path | None:
    """Return the remembered item-start linked git-dir for pinned rollback commands."""
    if not worktree_path.exists():
        return None
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return None
    text = _ITEM_START_GIT_LINKAGE.get(key)
    if text is None:
        return None
    return _resolve_gitfile_target(worktree_path, text)


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
            try:
                key = str(git_dir.resolve())
            except OSError:
                return None
            out[key] = dict(snap)
    return out


def _hash_local_git_config_snapshot(snapshot: dict[str, dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for git_dir in sorted(snapshot):
        configs = snapshot[git_dir]
        for name in sorted(configs):
            digest.update(git_dir.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(configs[name].encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint_from_git_config_snapshot(
    snapshot: dict[str, dict[str, str]],
    path_fingerprint: str,
) -> str:
    """Append ``git-meta:<sha256>`` so config-only mutations are visible."""
    meta_line = f"git-meta:{_hash_local_git_config_snapshot(snapshot)}"
    if not path_fingerprint:
        return meta_line
    return f"{path_fingerprint}\n{meta_line}"


async def _fingerprint_with_git_metadata(
    worktree_path: Path,
    path_fingerprint: str,
) -> str | None:
    """Append ``git-meta:<sha256>`` so config-only mutations are visible."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    try:
        snapshot = await _residue.asyncio.to_thread(
            _snapshot_worktree_local_git_configs,
            worktree_path,
        )
    except OSError:
        return None
    if snapshot is None:
        return None
    return _fingerprint_from_git_config_snapshot(snapshot, path_fingerprint)


def _fingerprint_has_pr_worthy_path_residue(fingerprint: str) -> bool:
    """True when fingerprint lines include porcelain/path residue.

    ``git-meta:`` and ``ignored:`` are mutation-identity lines, not PR-worthy
    dirt (ignored paths never enter the commit/PR).
    """
    return any(
        line.strip() and not line.startswith("git-meta:") and not line.startswith("ignored:")
        for line in fingerprint.splitlines()
    )


def _ignored_paths_from_status_stdout(status_stdout: str, *, is_z: bool) -> list[str]:
    """Extract ``!!`` ignored pathnames from porcelain status output."""
    if is_z:
        from awf.runtime.pr_monitor_runner.path_parsing import _porcelain_z_records

        return list(
            dict.fromkeys(
                path
                for status, path, _original in _porcelain_z_records(status_stdout)
                if status == "!!" and path
            )
        )
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.startswith("!! "):
            continue
        path = line[3:]
        if path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _hash_ignored_directory_metadata_residue(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Bounded overflow identity for an ignored directory (no full-body budget).

    Used when content hashing fails closed on the ordinary 32 MiB / entry budgets
    so typical large ignored roots (``node_modules/``, ``.venv/``) still produce
    a stable fingerprint instead of ``None`` (PRRT_kwDOSJAM6s6e4fPN). Name, mode,
    size, and per-file content (full body under the per-file cap; bounded
    head/tail beyond it) detect add/remove/resize and same-size overwrites that
    restore
    ``mtime_ns``, including middle-only edits (PRRT_kwDOSJAM6s6e5nwj /
    PRRT_kwDOSJAM6s6e65b4). Nested git checkouts reuse the trusted nested-worktree
    identity (HEAD / staged / unstaged / untracked) instead of a presence-only
    marker so edits inside an ignored nested checkout still change this
    fingerprint when the content digest falls back (PRRT_kwDOSJAM6s6e5mkg).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD,
        _git_nested_worktree_commit_at,
        _worktree_root_for_residue_byte_reads,
    )
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _WORKTREE_DIRECTORY_OPEN_FLAGS,
        _directory_enum_allows_descent,
        _has_nested_git_marker_at,
        _hash_regular_file_content_samples_into,
        _open_worktree_directory,
        _open_worktree_regular_file_at,
        _residue_directory_enum_budget,
        _sorted_worktree_directory_entry_names,
        _special_entry_blob_sha,
        _worktree_directory_entry_mode_token,
        _worktree_entry_kind,
        _worktree_entry_kind_at,
    )

    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None or kind_info[0] != "directory":
        return None

    def _hash_at(*, dir_fd: int, rel: str, depth: int) -> str | None:
        if not _directory_enum_allows_descent(depth):
            return None
        hasher = hashlib.sha256()
        entry_names = _sorted_worktree_directory_entry_names(dir_fd)
        if entry_names is None:
            return None
        for entry_name in entry_names:
            hasher.update(entry_name.encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
            child_kind = _worktree_entry_kind_at(dir_fd, entry_name)
            if child_kind is None:
                return None
            child_kind_name, child_mode = child_kind
            hasher.update(child_kind_name.encode("ascii"))
            hasher.update(b":")
            hasher.update(
                _worktree_directory_entry_mode_token(
                    kind=child_kind_name,
                    st_mode=child_mode,
                ).encode("ascii")
            )
            hasher.update(b"\0")
            if child_kind_name == "directory":
                try:
                    child_fd = os.open(entry_name, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
                except OSError:
                    return None
                try:
                    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                        return None
                    child_rel = f"{rel}/{entry_name}" if rel else entry_name
                    if _has_nested_git_marker_at(child_fd):
                        # Nested git checkouts: do not descend into object
                        # stores; fold in nested HEAD/staged/unstaged/untracked
                        # identity the same way content hashing does.
                        nested_id = _git_nested_worktree_commit_at(
                            dir_fd=child_fd,
                            git_env=git_env,
                            outer_worktree_path=worktree_path,
                        )
                        if nested_id is None:
                            return None
                        hasher.update(b"nested-git\0")
                        hasher.update(nested_id.encode("ascii"))
                    else:
                        nested = _hash_at(dir_fd=child_fd, rel=child_rel, depth=depth + 1)
                        if nested is None:
                            return None
                        hasher.update(nested.encode("ascii"))
                finally:
                    os.close(child_fd)
            elif child_kind_name == "regular":
                try:
                    st = os.lstat(entry_name, dir_fd=dir_fd)
                except OSError:
                    return None
                hasher.update(b"reg-sample\0")
                hasher.update(str(st.st_size).encode("ascii"))
                hasher.update(b"\0")
                try:
                    with _open_worktree_regular_file_at(dir_fd, entry_name) as fh:
                        if not _hash_regular_file_content_samples_into(hasher, fh):
                            return None
                except OSError:
                    return None
            elif child_kind_name == "symlink":
                try:
                    target = os.readlink(entry_name, dir_fd=dir_fd)
                except OSError:
                    return None
                hasher.update(b"symlink\0")
                hasher.update(target.encode("utf-8", errors="surrogateescape"))
                hasher.update(b"\0")
            else:
                hasher.update(
                    _special_entry_blob_sha(kind=child_kind_name, st_mode=child_mode).encode(
                        "ascii"
                    )
                )
            hasher.update(b"\0")
        return hasher.hexdigest()

    # Component-wise O_NOFOLLOW descent (same as content hashing): pathname
    # os.open(candidate, O_NOFOLLOW) only protects the final component, so an
    # intermediate directory symlink can redirect the walk outside the worktree
    # (PRRT_kwDOSJAM6s6e5o6e).
    with _residue_directory_enum_budget():
        try:
            with _open_worktree_directory(
                worktree_path,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            ) as root_fd:
                return _hash_at(dir_fd=root_fd, rel=path, depth=0)
        except OSError:
            return None


def _hash_ignored_residue_identity(
    *,
    worktree_path: Path,
    ignored_paths: list[str],
    git_env: dict[str, str],
) -> str | None:
    """Identity for porcelain ``!!`` paths (status with ``--ignored=matching``).

    File-level ignored entries are content-hashed like untracked residue.
    Directory entries (trailing slash) include a bounded content digest of the
    tree beneath them: Git reports only ``!! dir/`` before and after mutations
    under a pre-existing ignored root, so path identity alone would collide and
    leave rejected bytes behind after rollback (PRRT_kwDOSJAM6s6e4PhN). Digests
    reuse ``_hash_worktree_directory_residue`` (entry/depth/byte budgets). When
    that content digest fails closed on budget (typical large ignored roots),
    fall back to bounded name/mode/size/content identity (no aggregate byte
    budget; head/tail beyond the per-file cap) so clean non-FIXED corrections
    are not
    rejected as mutations (PRRT_kwDOSJAM6s6e4fPN) while same-size mtime-restored
    overwrites — including middle-only edits — still change the fingerprint
    (PRRT_kwDOSJAM6s6e5nwj / PRRT_kwDOSJAM6s6e65b4). Nested git checkouts under
    that overflow path still incorporate HEAD/staged/unstaged/untracked identity
    rather than a presence-only marker (PRRT_kwDOSJAM6s6e5mkg).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    if not ignored_paths:
        return hashlib.sha256().hexdigest()

    hasher = hashlib.sha256()
    file_paths = sorted(path for path in ignored_paths if not path.endswith("/"))
    dir_paths = sorted(path for path in ignored_paths if path.endswith("/"))
    for path in dir_paths:
        hasher.update(b"ignored-dir\0")
        hasher.update(path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        dir_rel = path.rstrip("/")
        if not dir_rel:
            return None
        dir_digest = _residue._hash_worktree_directory_residue(
            worktree_path=worktree_path,
            path=dir_rel,
            git_env=git_env,
        )
        if dir_digest is not None:
            hasher.update(b"ignored-dir-content\0")
            hasher.update(dir_digest.encode("ascii"))
            hasher.update(b"\0")
            continue
        meta_digest = _hash_ignored_directory_metadata_residue(
            worktree_path=worktree_path,
            path=dir_rel,
            git_env=git_env,
        )
        if meta_digest is None:
            return None
        hasher.update(b"ignored-dir-meta\0")
        hasher.update(meta_digest.encode("ascii"))
        hasher.update(b"\0")
    if file_paths:
        content_digest = _residue._hash_untracked_residue_paths(
            worktree_path=worktree_path,
            paths=file_paths,
            untracked=set(file_paths),
            git_env=git_env,
        )
        if content_digest is None:
            return None
        hasher.update(b"ignored-files\0")
        hasher.update(content_digest.encode("ascii"))
    return hasher.hexdigest()


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
    snapshot = _snapshot_worktree_local_git_configs(
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


def _restore_worktree_local_git_configs(snapshot: dict[str, dict[str, str]]) -> bool:
    """Rewrite snapshotted local configs and remove agent-created extras."""
    for git_dir_key, configs in snapshot.items():
        git_dir = Path(git_dir_key)
        for name in _LOCAL_GIT_CONFIG_NAMES:
            path = git_dir / name
            if name in configs:
                if not _write_local_git_config_file(path, configs[name]):
                    return False
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError:
                return False
            if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
                try:
                    path.unlink()
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
    return _restore_worktree_local_git_configs(snapshot)


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


def _symlink_git_store_child(live_git_dir: Path, name: str, dest: Path, *, required: bool) -> bool:
    """Symlink a live git-dir child into the trusted probe staging dir."""
    src = live_git_dir / name
    try:
        mode = src.lstat().st_mode
    except FileNotFoundError:
        return not required
    except OSError:
        return False
    try:
        resolved = src.resolve()
    except OSError:
        return False
    try:
        dest.symlink_to(resolved, target_is_directory=stat.S_ISDIR(mode))
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
    """Populate ``staging`` with trusted configs and live object/ref stores."""
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
    if not _symlink_git_store_child(live_git_dir, "objects", staging / "objects", required=True):
        return False
    if not _symlink_git_store_child(live_git_dir, "refs", staging / "refs", required=True):
        return False
    return _symlink_git_store_child(
        live_git_dir, "packed-refs", staging / "packed-refs", required=False
    )


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


def _porcelain_status_bytes_from_nul_records(records: tuple[bytes, ...]) -> bytes:
    if not records:
        return b""
    return b"\0".join(records) + b"\0"


async def _read_ordinary_porcelain_status(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    git_env: dict[str, str],
) -> CommandResult | None:
    """Read ordinary ``git status --porcelain -z`` without unbounded communicate().

    ``AsyncioSubprocessRunner.run`` materializes stdout via ``communicate()``
    before any caller-side byte check, so a path-name flood can pin hundreds of
    megabytes per concurrent monitor. Stream through the same capped NUL reader
    nested probes already use (PRRT_kwDOSJAM6s6eutWq). Test doubles still inject
    porcelain via ``runner.run``.
    """
    from awf.node.git_manager import (
        DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS,
        FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS,
        FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
        FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS,
        FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
    )
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    # Agent-set ``core.ignoreCase=true`` on a case-sensitive worker hides
    # ``FOO`` beside tracked ``foo`` from porcelain status (PRRT_kwDOSJAM6s6ex8lZ).
    # Agent-set ``core.fileMode=false`` hides executable-bit flips the same way
    # (PRRT_kwDOSJAM6s6ey_47); nested probes already force ``core.fileMode=true``.
    # Agent-set ``core.symlinks=false`` hides symlink→file typechanges the same
    # way (PRRT_kwDOSJAM6s6ezrHU); nested probes already force ``core.symlinks=true``.
    # Agent-set ``core.fsmonitor`` can prime then omit tracked edits from status
    # (PRRT_kwDOSJAM6s6e0BJS); nested probes already clear ``core.fsmonitor``.
    # Agent-set ``core.trustctime=false`` / ``core.checkStat=minimal`` can omit
    # same-size mtime-restored overwrites from status (PRRT_kwDOSJAM6s6e1yPZ).
    # ``--ignored=matching`` surfaces self-hiding ``.gitignore`` residue that
    # ordinary porcelain omits (PRRT_kwDOSJAM6s6e3D-C); nested probes already
    # list ignored entries via ``ls-files -o`` without ``--exclude-standard``.
    command = git_worktree_command(
        worktree_path,
        *FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS,
        *FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
        *FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
        *DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS,
        *FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
    )
    runner_impl = runner._deps.runner
    if isinstance(runner_impl, AsyncioSubprocessRunner):
        records = await _residue.asyncio.to_thread(
            _residue._run_ordinary_porcelain_status_capped,
            command,
            git_env=git_env,
        )
        if records is None:
            return None
        raw = _porcelain_status_bytes_from_nul_records(records)
        return CommandResult(
            returncode=0,
            stdout=raw.decode("utf-8", errors="surrogateescape"),
            stderr="",
            stdout_bytes=raw,
        )
    return await runner_impl.run(
        command,
        env=git_env,
        timeout_seconds=_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )


async def _read_correction_pr_worthy_residue_fingerprint(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Return a fingerprint of PR-worthy dirty porcelain plus local Git metadata.

    Empty string means missing worktree. A ``git-meta:<sha256>``-only value means
    clean path residue. ``None`` means the status/metadata probe failed and
    callers must fail closed. Untracked AWF-agent-runtime paths are excluded,
    matching the commit sink's dirtiness filter.

    Path names alone are not enough: when attempt 0 leaves ``src/x.py`` dirty and
    the correction edits that same file, a path-only fingerprint collides and
    attribution treats the mutation as pre-existing residue
    (PRRT_kwDOSJAM6s6eKj9D). Include staged/unstaged diff hashes and untracked
    file content identity while retaining the runtime-path exclusion.

    Local Git config is included so config-only mutations (for example
    ``url.*.insteadOf``) cannot collide with a clean fingerprint
    (PRRT_kwDOSJAM6s6e0Xdl).

    Ignored (``!!``) entries from ``--ignored=matching`` are fingerprinted as
    ``ignored:<sha256>`` so a self-hiding ``.gitignore`` cannot hide correction
    residue from mutation detection (PRRT_kwDOSJAM6s6e3D-C). Directory entries
    include a bounded content digest so mutations under a pre-existing ignored
    root cannot collide with the correction-start fingerprint
    (PRRT_kwDOSJAM6s6e4PhN). That line is not PR-worthy path residue.
    """
    # Resolve helpers via the residue module object so monkeypatches on
    # ``comment_verdict_residue`` (asyncio.to_thread, hash callees, scan budget)
    # continue to apply after this split.
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    if not worktree_path.exists():
        return ""

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner.path_parsing import (
        _changed_paths_from_porcelain,
        _changed_paths_from_porcelain_z,
        _porcelain_z_records,
        _untracked_paths_from_porcelain,
        _untracked_paths_from_porcelain_z,
    )
    from awf.runtime.validation_worktree import is_under_agent_runtime_root

    git_env = git_env_without_object_lookup_overrides()

    try:
        status = await _read_ordinary_porcelain_status(
            runner,
            worktree_path=worktree_path,
            git_env=git_env,
        )
    except OSError as exc:
        # Spawn failures (e.g. OSError from create_subprocess_exec) must fail
        # closed like a non-ok status so the correction mutation path rolls back
        # unaccepted dirty edits (PRRT_kwDOSJAM6s6eJi5X). Do not swallow
        # programming errors such as TypeError (review 5096023656).
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=redact_secrets(str(exc))[:400],
        )
        return None
    if status is None or not status.ok:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            returncode=None if status is None else status.returncode,
            stderr="" if status is None else redact_secrets(status.stderr or "")[:400],
        )
        return None

    status_stdout, is_z = _decode_porcelain_status_stdout(
        stdout=status.stdout or "",
        stdout_bytes=status.stdout_bytes,
    )
    raw_status = (
        status.stdout_bytes
        if status.stdout_bytes is not None
        else status_stdout.encode("utf-8", errors="surrogateescape")
    )
    if len(raw_status) > _residue._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            stdout_bytes=len(raw_status),
        )
        return None
    if is_z:
        if status.stdout_bytes is not None and not status.stdout_bytes.strip(b"\0"):
            return await _fingerprint_with_git_metadata(worktree_path, "")
        if (
            status.stdout_bytes is None and not status_stdout.strip()
        ):  # pragma: no cover - NUL survives strip
            return await _fingerprint_with_git_metadata(worktree_path, "")
    elif not status_stdout.strip():
        return await _fingerprint_with_git_metadata(worktree_path, "")

    ignored_paths = sorted(
        path
        for path in _ignored_paths_from_status_stdout(status_stdout, is_z=is_z)
        if not is_under_agent_runtime_root(path)
    )

    async def _ignored_digest_or_none() -> str | None:
        if not ignored_paths:
            return None
        try:
            with _residue._residue_fingerprint_nested_scan_budget():
                return await _residue.asyncio.to_thread(
                    _hash_ignored_residue_identity,
                    worktree_path=worktree_path,
                    ignored_paths=ignored_paths,
                    git_env=git_env,
                )
        except OSError as exc:
            _log.warning(
                "monitor.agent_verdict_correction_residue_ignored_failed",
                workspace_id=workspace_id,
                exc_type=type(exc).__name__,
                error=redact_secrets(str(exc))[:400],
            )
            return None

    async def _with_ignored(path_fingerprint: str, ignored_digest: str | None) -> str | None:
        if ignored_digest is None and not ignored_paths:
            return await _fingerprint_with_git_metadata(worktree_path, path_fingerprint)
        if ignored_digest is None:
            return None
        if path_fingerprint:
            return await _fingerprint_with_git_metadata(
                worktree_path,
                f"{path_fingerprint}\nignored:{ignored_digest}",
            )
        return await _fingerprint_with_git_metadata(worktree_path, f"ignored:{ignored_digest}")

    if is_z:
        untracked = set(_untracked_paths_from_porcelain_z(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain_z(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    else:
        untracked = set(_untracked_paths_from_porcelain(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    ignored_digest: str | None = None
    if not paths:
        ignored_digest = await _ignored_digest_or_none()
        if ignored_paths and ignored_digest is None:
            _log.warning(
                "monitor.agent_verdict_correction_residue_ignored_unreadable",
                workspace_id=workspace_id,
            )
            return None
        return await _with_ignored("", ignored_digest)

    tracked_paths = [path for path in paths if path not in untracked]

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    if is_z:
        status_lines = sorted(
            _format_porcelain_z_line(status_code, path, original_path)
            for status_code, path, original_path in _porcelain_z_records(status_stdout)
            if path in path_set or (original_path is not None and original_path in path_set)
        )
    else:
        status_lines = sorted(
            line
            for line in status_stdout.splitlines()
            if line
            and any(
                candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n")
            )
        )

    try:
        with _residue._residue_fingerprint_nested_scan_budget():
            if tracked_paths:
                staged_digest, unstaged_digest = await _residue.asyncio.to_thread(
                    _residue._hash_tracked_residue_staged_and_unstaged,
                    worktree_path=worktree_path,
                    git_env=git_env,
                )
            else:
                empty_digest = hashlib.sha256().hexdigest()
                staged_digest = unstaged_digest = empty_digest
            if staged_digest is None or unstaged_digest is None:
                _log.warning(
                    "monitor.agent_verdict_correction_residue_diff_failed",
                    workspace_id=workspace_id,
                    staged_digest=staged_digest,
                    unstaged_digest=unstaged_digest,
                )
                return None

            try:
                untracked_digest = await _residue.asyncio.to_thread(
                    _residue._hash_untracked_residue_paths,
                    worktree_path=worktree_path,
                    paths=paths,
                    untracked=untracked,
                    git_env=git_env,
                )
            except OSError as exc:
                # Hash helpers raise OSError on spawn/IO failure; programming
                # errors must propagate (review 5096023656).
                _log.warning(
                    "monitor.agent_verdict_correction_residue_untracked_failed",
                    workspace_id=workspace_id,
                    exc_type=type(exc).__name__,
                    error=redact_secrets(str(exc))[:400],
                )
                return None
            if ignored_paths:
                try:
                    ignored_digest = await _residue.asyncio.to_thread(
                        _hash_ignored_residue_identity,
                        worktree_path=worktree_path,
                        ignored_paths=ignored_paths,
                        git_env=git_env,
                    )
                except OSError as exc:
                    _log.warning(
                        "monitor.agent_verdict_correction_residue_ignored_failed",
                        workspace_id=workspace_id,
                        exc_type=type(exc).__name__,
                        error=redact_secrets(str(exc))[:400],
                    )
                    return None
    except OSError as exc:
        _log.warning(
            "monitor.agent_verdict_correction_residue_diff_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=redact_secrets(str(exc))[:400],
        )
        return None
    if untracked_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_untracked_unreadable",
            workspace_id=workspace_id,
        )
        return None
    if ignored_paths and ignored_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_ignored_unreadable",
            workspace_id=workspace_id,
        )
        return None

    path_fingerprint = "\n".join(
        [
            *status_lines,
            f"staged:{staged_digest}",
            f"unstaged:{unstaged_digest}",
            f"untracked:{untracked_digest}",
        ]
    )
    return await _with_ignored(path_fingerprint, ignored_digest)


def _correction_authored_mutation_vs_start(
    *,
    attempt_start_head: str | None,
    pre_sink_head: str | None,
    correction_start_residue_fp: str | None,
    pre_sink_residue_fp: str | None,
) -> bool:
    """True when the correction agent mutated HEAD or dirt before the commit sink."""
    if pre_sink_head is None:
        # Cannot observe pre-sink HEAD — fail closed (PRRT_kwDOSJAM6s6eKoIe).
        return True
    if attempt_start_head is not None and pre_sink_head.lower() != attempt_start_head.lower():
        return True
    if pre_sink_residue_fp is None:
        # Cannot observe post-agent dirt — fail closed.
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: dirty-to-clean correction mutations are
        # unverifiable (PRRT_kwDOSJAM6s6eU900).
        return True
    return pre_sink_residue_fp != correction_start_residue_fp


def _stranded_residue_is_correction_mutation(
    *,
    correction_start_residue_fp: str | None,
    post_residue_fp: str | None,
) -> bool:
    """True when post-sink stranded dirt is not attributable to correction-start."""
    if post_residue_fp is None:
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: empty post-sink residue cannot prove no correction
        # mutation (PRRT_kwDOSJAM6s6eU900).
        return True
    return post_residue_fp != correction_start_residue_fp


async def _correction_attempt_left_pr_worthy_residue(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """True when uncommitted PR-worthy dirt remains after the commit sink.

    ``_commit_dirty_worktree`` may return False after status/add/commit failure
    while leaving correction edits dirty. HEAD can stay at attempt-start with
    ``dirty_changes_committed`` False, so mutation detection must probe porcelain
    before rollback accepts a non-FIXED correction verdict. Status inspection
    failure fails closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.
    """
    fingerprint = await _read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    if fingerprint is None:
        return True
    return _fingerprint_has_pr_worthy_path_residue(fingerprint)
