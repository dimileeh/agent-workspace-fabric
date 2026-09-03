"""Correction-attempt residue hashing and nested-probe helpers.

Correction fingerprint / mutation predicates live in
``comment_verdict_residue_fingerprint`` and are re-exported here for callers
and tests that still import them from this module.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib
import errno
import hashlib
import os
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from pathlib import Path

from awf.node.git_manager import (
    git_env_for_untrusted_nested_repository_probe,
    untrusted_nested_probe_config_snapshot_git_dir,
    untrusted_nested_repository_local_config_has_includes,
)
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_fingerprint as _residue_fingerprint,
)
from awf.runtime.pr_monitor_runner import (
    comment_verdict_residue_tracked as _residue_tracked,
)
from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
    _NESTED_UNTRACKED_LS_FILES_MAX_STDOUT_BYTES,
    _SPECIAL_ENTRY_KINDS,
    _WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES,
    _WORKTREE_DIRECTORY_OPEN_FLAGS,
    _directory_enum_allows_descent,
    _directory_enum_consume_entries,
    _fresh_worktree_path_for_open_fd,
    _has_nested_git_marker,
    _has_nested_git_marker_at,
    _hash_opened_regular_file_into,
    _NestedProbeDeadline,
    _open_worktree_directory,
    _open_worktree_directory_path,
    _open_worktree_regular_file_at,
    _open_worktree_regular_file_under_root,
    _popen_capped_nul_path_records,
    _read_opened_regular_file_snapshot,
    _read_worktree_symlink_under_root,
    _residue_directory_enum_budget,
    _residue_regular_hash_budget,
    _sorted_worktree_directory_entry_names,
    _special_entry_blob_sha,
    _worktree_directory_entry_mode_token,
    _worktree_entry_kind,
    _worktree_entry_kind_at,
    _worktree_mode_from_kind,
    _worktree_proc_path_for_open_fd,
)
from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
    _approved_git_metadata_roots,
    _nested_probe_root_within_outer_worktree,
    _open_nested_git_dir_gitfile_target_at,
    _open_nested_git_dir_marker_at,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_untrusted_nested_pinned_worktree_command,
    git_untrusted_nested_snapshot_discovery_command,
    git_untrusted_nested_worktree_command,
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.path_helpers import (
    _changed_paths_from_name_only_z as _path_helpers_changed_paths_from_name_only_z,
)

_correction_authored_mutation_vs_start = _residue_fingerprint._correction_authored_mutation_vs_start
_correction_attempt_left_pr_worthy_residue = (
    _residue_fingerprint._correction_attempt_left_pr_worthy_residue
)
_decode_porcelain_status_stdout = _residue_fingerprint._decode_porcelain_status_stdout
_format_porcelain_z_line = _residue_fingerprint._format_porcelain_z_line
_read_correction_pr_worthy_residue_fingerprint = (
    _residue_fingerprint._read_correction_pr_worthy_residue_fingerprint
)
_stranded_residue_is_correction_mutation = (
    _residue_fingerprint._stranded_residue_is_correction_mutation
)
_BytesHasher = _residue_tracked._BytesHasher
_git_index_mode = _residue_tracked._git_index_mode
_hash_index_stage_entries = _residue_tracked._hash_index_stage_entries
_hash_tracked_residue_diffs = _residue_tracked._hash_tracked_residue_diffs
_hash_tracked_residue_staged_and_unstaged = (
    _residue_tracked._hash_tracked_residue_staged_and_unstaged
)
_load_git_index_stage_map = _residue_tracked._load_git_index_stage_map
_parse_git_index_stage_records = _residue_tracked._parse_git_index_stage_records
_representative_index_stage = _residue_tracked._representative_index_stage
# Re-export for facade monkeypatches used by tracked hashing (and tests).
_changed_paths_from_name_only_z = _path_helpers_changed_paths_from_name_only_z

_UNBORN_HEAD_SENTINEL = "<unborn>"
_NESTED_UNTRUSTED_GIT_PROBE: ContextVar[bool] = ContextVar(
    "_nested_untrusted_git_probe",
    default=False,
)
_NESTED_FINGERPRINT_SCAN_ACTIVE: ContextVar[int] = ContextVar(
    "_nested_fingerprint_scan_active",
    default=0,
)
_NESTED_UNTRUSTED_GIT_PROBE_DEADLINE: ContextVar[_NestedProbeDeadline | None] = ContextVar(
    "_nested_untrusted_git_probe_deadline",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_git_dir",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_git_marker_fd",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_git_common_fd",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_worktree",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD: ContextVar[int | None] = ContextVar(
    "_nested_untrusted_git_probe_worktree_fd",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR: ContextVar[Path | None] = ContextVar(
    "_nested_untrusted_git_probe_config_snapshot_git_dir",
    default=None,
)
_NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS = 30.0
_NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS = 30.0
# Ordinary (non-nested) fingerprint Git probes get a separate deadline so parent
# hashing cannot consume the nested-probe scan budget.
_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS = 30.0
# Nested path listings share the directory-enum entry budget
# (PRRT_kwDOSJAM6s6efXeI / PRRT_kwDOSJAM6s6ef8Fs).
_NESTED_UNTRACKED_LS_FILES_MAX_PATHS = _WORKTREE_DIRECTORY_ENUM_AGGREGATE_MAX_ENTRIES
# Ordinary fingerprint Git stdout is capped at the same byte scale as nested
# NUL listings so path floods cannot buffer unbounded porcelain.
_RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES = _NESTED_UNTRACKED_LS_FILES_MAX_STDOUT_BYTES
# Keep ``ls-files --stage -- <paths>`` argv chunks well under ARG_MAX when many
# dirty paths are fingerprinted (PRRT_kwDOSJAM6s6ewISJ).
_INDEX_STAGE_LS_FILES_PATH_CHUNK = 1024
# Aggregate wall budget for ordinary (non-nested) fingerprint Git probes so
# per-path hashing cannot monopolize monitor workers (PRRT_kwDOSJAM6s6evsYB).
_ORDINARY_FINGERPRINT_GIT_DEADLINE: ContextVar[_NestedProbeDeadline | None] = ContextVar(
    "_ordinary_fingerprint_git_deadline",
    default=None,
)


def _run_ordinary_porcelain_status_capped(
    command: list[str],
    *,
    git_env: Mapping[str, str],
) -> tuple[bytes, ...] | None:
    """
    Stream ordinary Git porcelain status output as NUL-delimited path records.
    
    Returns:
        tuple[bytes, ...] | None: The status path records, or `None` if the
        command fails or exceeds its configured output or time limits.
    """
    return _popen_capped_nul_path_records(
        command,
        env=dict(git_env),
        max_records=_NESTED_UNTRACKED_LS_FILES_MAX_PATHS,
        max_bytes=_RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES,
        timeout=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )


def _nested_untrusted_git_probe_remaining_seconds() -> float | None:
    """
    Determine the remaining time allowed for the current untrusted nested Git probe.
    
    Returns:
    	float | None: The remaining time in seconds, or `None` when no probe deadline is active.
    """
    holder = _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.get()
    if holder is None or holder.deadline is None:
        return None
    return max(0.0, holder.deadline - time.monotonic())


@contextlib.contextmanager
def _nested_probe_config_snapshot_git_dir(snapshot_git_dir: Path) -> Iterator[None]:
    """Pin nested probe Git commands to a validated local-config snapshot git-dir."""
    token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.set(
        snapshot_git_dir
    )
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.reset(token)


def _nested_untrusted_git_probe_past_deadline() -> bool:
    """Determine whether the deadline for the current untrusted nested Git probe has passed.
    
    Returns:
    	bool: `true` if the probe deadline has passed, `false` otherwise.
    """
    remaining = _nested_untrusted_git_probe_remaining_seconds()
    return remaining is not None and remaining <= 0.0


def _nested_untrusted_git_probe_command_timeout() -> float | None:
    """
    Determine the timeout to apply to an untrusted nested Git probe.
    
    Returns:
    	float | None: The remaining probe timeout, capped at the configured limit; `0.0` when the deadline has expired; or `None` when no untrusted nested probe is active.
    """
    if not _NESTED_UNTRUSTED_GIT_PROBE.get():
        return None
    remaining = _nested_untrusted_git_probe_remaining_seconds()
    if remaining is not None:
        if remaining <= 0.0:
            return 0.0
        return min(_NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS, remaining)
    return _NESTED_UNTRUSTED_GIT_PROBE_TIMEOUT_SECONDS


def _ordinary_fingerprint_git_remaining_seconds() -> float | None:
    """Return the remaining time allowed for the ordinary fingerprint Git operation.
    
    Returns:
        float | None: The remaining time in seconds, or `None` when no deadline is set.
    """
    holder = _ORDINARY_FINGERPRINT_GIT_DEADLINE.get()
    if holder is None or holder.deadline is None:
        return None
    return max(0.0, holder.deadline - time.monotonic())


def _ordinary_fingerprint_git_past_deadline() -> bool:
    """Determine whether the ordinary fingerprinting Git deadline has elapsed.
    
    Returns:
    	bool: `True` if the deadline has elapsed, `False` otherwise.
    """
    remaining = _ordinary_fingerprint_git_remaining_seconds()
    return remaining is not None and remaining <= 0.0


def _ordinary_fingerprint_git_command_timeout() -> float | None:
    """
    Determine the Git command timeout for the ordinary fingerprint scan.
    
    Returns:
        The remaining timeout in seconds, capped at the configured ordinary
        Git timeout, or `0.0` when the scan budget has expired. Returns `None`
        when no applicable scan budget is active.
    """
    if not _NESTED_FINGERPRINT_SCAN_ACTIVE.get() or _NESTED_UNTRUSTED_GIT_PROBE.get():
        return None
    remaining = _ordinary_fingerprint_git_remaining_seconds()
    if remaining is not None:
        if remaining <= 0.0:
            return 0.0
        return min(_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS, remaining)
    return _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS


def _residue_git_probe_command_timeout() -> float | None:
    """Return the active Git probe timeout for nested or ordinary fingerprint operations.
    
    Returns:
    	float | None: The nested-probe timeout when one is active; otherwise, the remaining ordinary fingerprint command timeout, or `None` when no timeout is configured.
    """
    timeout = _nested_untrusted_git_probe_command_timeout()
    if timeout is not None:
        return timeout
    return _ordinary_fingerprint_git_command_timeout()


@contextlib.contextmanager
def _residue_fingerprint_nested_scan_budget() -> Iterator[None]:
    """
    Bound nested Git probes, regular-file hashing, and directory enumeration across a fingerprint scan.
    """
    token: Token[int] = _NESTED_FINGERPRINT_SCAN_ACTIVE.set(
        _NESTED_FINGERPRINT_SCAN_ACTIVE.get() + 1
    )
    is_outermost = _NESTED_FINGERPRINT_SCAN_ACTIVE.get() == 1
    deadline_token: Token[_NestedProbeDeadline | None] | None = None
    ordinary_deadline_token: Token[_NestedProbeDeadline | None] | None = None
    if is_outermost:
        # Install a mutable holder before any ``to_thread`` so tracked and
        # untracked workers share one lazy deadline (PRRT_kwDOSJAM6s6eglyo).
        deadline_token = _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.set(_NestedProbeDeadline())
        # Ordinary fingerprint Git probes share one eager aggregate deadline
        # separate from the nested-probe budget (PRRT_kwDOSJAM6s6evsYB).
        ordinary_holder = _NestedProbeDeadline()
        ordinary_holder.deadline = time.monotonic() + _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
        ordinary_deadline_token = _ORDINARY_FINGERPRINT_GIT_DEADLINE.set(ordinary_holder)
    hash_budget = _residue_regular_hash_budget() if is_outermost else contextlib.nullcontext()
    enum_budget = _residue_directory_enum_budget() if is_outermost else contextlib.nullcontext()
    try:
        with hash_budget, enum_budget:
            yield
    finally:
        _NESTED_FINGERPRINT_SCAN_ACTIVE.reset(token)
        if ordinary_deadline_token is not None:
            _ORDINARY_FINGERPRINT_GIT_DEADLINE.reset(ordinary_deadline_token)
        if deadline_token is not None:
            _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.reset(deadline_token)


@contextlib.contextmanager
def _untrusted_nested_git_probe() -> Iterator[None]:
    """Scope nested embedded-repo Git probes to sanitized config and bounded runtime."""
    token: Token[bool] = _NESTED_UNTRUSTED_GIT_PROBE.set(True)
    holder = _NESTED_UNTRUSTED_GIT_PROBE_DEADLINE.get()
    if holder is not None and holder.deadline is None:
        holder.deadline = time.monotonic() + _NESTED_UNTRUSTED_GIT_PROBE_SCAN_BUDGET_SECONDS
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE.reset(token)


def _git_command_for_residue_probe(worktree_path: Path, *args: str) -> list[str]:
    """
    Select the Git command appropriate for the current residue-probe context.
    
    Parameters:
    	worktree_path (Path): Worktree path used when no pinned worktree is available.
    
    Returns:
    	list[str]: Git command and arguments for the active probe context.
    """
    snapshot_git_dir = _NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.get()
    pinned_worktree = _fresh_pinned_nested_worktree()
    if snapshot_git_dir is not None:
        # Validated local-config snapshot: never re-open live ``$GIT_DIR/config``
        # (PRRT_kwDOSJAM6s6elv_p).
        if pinned_worktree is not None:
            return git_untrusted_nested_pinned_worktree_command(
                snapshot_git_dir,
                pinned_worktree,
                *args,
            )
        # Discovery must honor snapshotted ``core.worktree`` (no ``--work-tree``).
        return git_untrusted_nested_snapshot_discovery_command(
            snapshot_git_dir,
            worktree_path,
            *args,
        )
    pinned_git_dir = _fresh_pinned_nested_git_dir()
    if pinned_git_dir is not None and pinned_worktree is not None:
        return git_untrusted_nested_pinned_worktree_command(
            pinned_git_dir,
            pinned_worktree,
            *args,
        )
    if _NESTED_UNTRUSTED_GIT_PROBE.get():
        return git_untrusted_nested_worktree_command(worktree_path, *args)
    return git_worktree_command(worktree_path, *args)


def _fresh_pinned_nested_git_dir() -> Path | None:
    """Return the pinned nested git-dir path via an open marker or gitfile-target fd."""
    marker_fd = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.get()
    if marker_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(marker_fd)
        if proc_path is None:
            return None
        try:
            return proc_path.readlink()
        except OSError:
            return None
    return _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.get()


def _fresh_pinned_nested_git_common_dir() -> Path | None:
    """Return the pinned nested common-dir path via an open approved ``commondir`` fd."""
    common_fd = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.get()
    if common_fd is None:
        return None
    proc_path = _worktree_proc_path_for_open_fd(common_fd)
    if proc_path is None:
        return None
    try:
        return proc_path.readlink()
    except OSError:
        return None


def _fresh_pinned_nested_worktree() -> Path | None:
    """
    Resolve the pinned nested work-tree path for Git operations.
    
    Returns:
    	Path | None: The resolved work-tree path, or `None` when no valid pinned path is available.
    """
    worktree_fd = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get()
    if worktree_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(worktree_fd)
        if proc_path is None:
            return None
        try:
            return proc_path.readlink()
        except OSError:
            return None
    return _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.get()


def _worktree_root_for_residue_byte_reads(worktree_path: Path) -> Path:
    """Return a worktree root for content reads that stays on the pinned inode.

    When a nested worktree directory fd is held, prefer ``/proc/self/fd/<fd>``
    so multi-file / multi-gigabyte hashing cannot follow a pathname replacement
    of the effective worktree (PRRT_kwDOSJAM6s6eajOa).
    """
    worktree_fd = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get()
    if worktree_fd is not None:
        proc_path = _worktree_proc_path_for_open_fd(worktree_fd)
        if proc_path is not None:
            return proc_path
    return worktree_path


@contextlib.contextmanager
def _pinned_nested_worktree_fd(worktree_fd: int) -> Iterator[None]:
    """Retain an opened effective worktree directory fd for nested residue probes."""
    token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(worktree_fd)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(token)


@contextlib.contextmanager
def _pinned_nested_git_probe(git_dir: Path, worktree_path: Path) -> Iterator[None]:
    """Pin nested embedded-repo probes to a specific git-dir and work-tree."""
    git_dir_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.set(git_dir)
    worktree_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.set(worktree_path)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.reset(git_dir_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.reset(worktree_token)


@contextlib.contextmanager
def _without_nested_git_probe_pin() -> Iterator[None]:
    """Clear nested git-dir/work-tree/marker pins so inner-repo discovery is not mis-scoped."""
    git_dir_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.set(None)
    worktree_token: Token[Path | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.set(None)
    worktree_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.set(None)
    marker_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(None)
    common_fd_token: Token[int | None] = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(None)
    try:
        yield
    finally:
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.reset(worktree_fd_token)
        _NESTED_UNTRUSTED_GIT_PROBE_GIT_DIR.reset(git_dir_token)
        _NESTED_UNTRUSTED_GIT_PROBE_WORKTREE.reset(worktree_token)


def _digest_worktree_entry_bytes(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> bytes | None:
    """
    Compute a SHA-256 digest for a worktree entry.
    
    Parameters:
        worktree_path (Path): Root path of the worktree.
        path (str): Relative path of the entry within the worktree.
        git_env (Mapping[str, str]): Environment used for nested Git operations.
    
    Returns:
        bytes | None: The entry digest, or `None` if the entry is missing, unreadable, invalid, or cannot be safely processed.
    """
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, st_mode = kind_info
    hasher = hashlib.sha256()

    if kind == "symlink":
        try:
            # Component-wise no-follow parent walk + dir_fd readlink
            # (PRRT_kwDOSJAM6s6eiJk-); pathname readlink follows mid-path swaps.
            link_text = _read_worktree_symlink_under_root(
                byte_root,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            )
        except OSError:
            return None
        hasher.update(b"symlink:")
        # Use the already-classified st_mode from the pinned byte_root walk;
        # a second pathname lstat can diverge under concurrent swaps
        # (review 5096023656).
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        hasher.update(link_text)
    elif kind == "regular":
        hasher.update(b"regular:")
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        try:
            with _open_worktree_regular_file_under_root(
                byte_root,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            ) as fh:
                # Bound open-time size (PRRT_kwDOSJAM6s6ecabJ); component-wise
                # no-follow open (PRRT_kwDOSJAM6s6ef8Fg).
                if not _hash_opened_regular_file_into(hasher, fh):
                    return None
        except OSError:
            return None
    elif kind == "directory":
        if _has_nested_git_marker(candidate):
            nested = _git_nested_worktree_commit(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            if nested is None:
                return None
            hasher.update(b"nested-git:")
            hasher.update(nested.encode("ascii"))
        else:
            directory_fp = _hash_worktree_directory_residue(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            if directory_fp is None:
                return None
            hasher.update(b"directory:")
            hasher.update(directory_fp.encode("ascii"))
    elif kind in _SPECIAL_ENTRY_KINDS:
        hasher.update(kind.encode("ascii"))
        hasher.update(b":")
        hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    else:  # pragma: no cover - kinds always come from _worktree_entry_kind_from_mode
        return None
    return hasher.digest()


def _digest_worktree_entry_bytes_at(
    *,
    dir_fd: int,
    entry_name: str,
    path: str,
    worktree_path: Path,
) -> bytes | None:
    """Digest a worktree entry using its open parent directory descriptor.
    
    The digest includes the entry kind, applicable mode, and content or link text. Returns `None` if the entry cannot be safely identified or read.
    
    Parameters:
        dir_fd (int): File descriptor for the entry's parent directory.
        entry_name (str): Name of the entry within the parent directory.
        path (str): Worktree-relative path used for fallback mode resolution.
        worktree_path (Path): Root path of the worktree used for fallback mode resolution.
    
    Returns:
        bytes | None: The SHA-256 digest of the entry, or `None` if it cannot be safely read or identified.
    """
    kind_info = _worktree_entry_kind_at(dir_fd, entry_name)
    if kind_info is None:
        return None
    kind, st_mode = kind_info
    hasher = hashlib.sha256()

    if kind == "symlink":
        try:
            link_text = os.readlink(entry_name, dir_fd=dir_fd).encode(
                "utf-8", errors="surrogateescape"
            )
        except OSError:
            return None
        hasher.update(b"symlink:")
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        if worktree_mode is None:  # pragma: no cover - symlink always maps to 120000
            worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        hasher.update(link_text)
    elif kind == "regular":
        hasher.update(b"regular:")
        worktree_mode = _worktree_mode_from_kind(kind=kind, st_mode=st_mode)
        if worktree_mode is None:  # pragma: no cover - regular always maps to 100644/100755
            worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
        hasher.update(b"mode:")
        hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
        try:
            with _open_worktree_regular_file_at(dir_fd, entry_name) as fh:
                # Bound to the open-time size and revalidate so a concurrent
                # appender cannot stretch the read loop forever
                # (PRRT_kwDOSJAM6s6ecabJ).
                if not _hash_opened_regular_file_into(hasher, fh):
                    return None
        except OSError:
            return None
    elif kind in _SPECIAL_ENTRY_KINDS:
        hasher.update(kind.encode("ascii"))
        hasher.update(b":")
        hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    else:  # pragma: no cover - kinds always come from _worktree_entry_kind_from_mode
        return None
    return hasher.digest()


def _hash_worktree_directory_residue_at_dir_fd(
    *,
    worktree_path: Path,
    path: str,
    dir_fd: int,
    git_env: Mapping[str, str],
    depth: int = 0,
) -> str | None:
    """
    Compute a digest for a worktree directory and its recursively hashed entries.
    
    Parameters:
    	worktree_path (Path): Path to the containing worktree.
    	path (str): Logical path of the directory being hashed.
    	dir_fd (int): File descriptor for the directory.
    	git_env (Mapping[str, str]): Environment used for nested Git probes.
    	depth (int): Current recursion depth.
    
    Returns:
    	str | None: The directory digest, or `None` if traversal fails or exceeds the directory-enumeration budget.
    """
    if not _directory_enum_allows_descent(depth):
        return None
    hasher = hashlib.sha256()
    entry_names = _sorted_worktree_directory_entry_names(dir_fd)
    if entry_names is None:
        return None

    for entry_name in entry_names:
        child_path = f"{path}/{entry_name}"
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
                if _has_nested_git_marker_at(child_fd):
                    nested = _git_nested_worktree_commit_at(
                        dir_fd=child_fd,
                        git_env=git_env,
                        outer_worktree_path=worktree_path,
                    )
                    if nested is None:
                        return None
                    hasher.update(nested.encode("ascii"))
                else:
                    nested_dir = _hash_worktree_directory_residue_at_dir_fd(
                        worktree_path=worktree_path,
                        path=child_path,
                        dir_fd=child_fd,
                        git_env=git_env,
                        depth=depth + 1,
                    )
                    if nested_dir is None:
                        return None
                    hasher.update(nested_dir.encode("ascii"))
            finally:
                os.close(child_fd)
        else:
            child_digest = _digest_worktree_entry_bytes_at(
                dir_fd=dir_fd,
                entry_name=entry_name,
                path=child_path,
                worktree_path=worktree_path,
            )
            if child_digest is None:
                return None
            hasher.update(child_digest)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_worktree_directory_residue(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """
    Hash the contents of a worktree directory for residue fingerprinting.
    
    Parameters:
    	worktree_path (Path): The worktree containing the directory.
    	path (str): The directory path relative to the worktree.
    	git_env (Mapping[str, str]): Environment variables used during fingerprinting.
    
    Returns:
    	str | None: The directory residue digest, or None if the path is not a directory or cannot be read safely.
    """
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None or kind_info[0] != "directory":
        return None

    def _hash_opened() -> str | None:
        """
        Hash the worktree residue at an opened path.
        
        Returns:
            str | None: The residue digest, or `None` if the path cannot be opened or read.
        """
        try:
            with _open_worktree_directory(
                worktree_path,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            ) as dir_fd:
                return _hash_worktree_directory_residue_at_dir_fd(
                    worktree_path=worktree_path,
                    path=path,
                    dir_fd=dir_fd,
                    git_env=git_env,
                )
        except OSError:
            return None

    # Always bound empty-directory scans even when callers omit the fingerprint
    # nested-scan budget (PRRT_kwDOSJAM6s6eeAsN).
    with _residue_directory_enum_budget():
        return _hash_opened()


def _hash_untracked_residue_paths(
    *,
    worktree_path: Path,
    paths: list[str],
    untracked: set[str],
    git_env: Mapping[str, str] | None = None,
) -> str | None:
    """
    Compute a digest for selected untracked paths and their contents.
    
    Parameters:
        worktree_path (Path): Root path of the worktree containing the residue.
        paths (list[str]): Paths to consider for hashing.
        untracked (set[str]): Paths currently identified as untracked.
        git_env (Mapping[str, str] | None): Optional environment for Git operations.
    
    Returns:
        str | None: Hexadecimal digest of the selected residue, or ``None`` if any
        residue cannot be read consistently.
    """
    untracked_hasher = hashlib.sha256()
    env = dict(git_env or {})
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    for path in paths:
        if path not in untracked:
            continue
        # Hash each file independently so raw bytes cannot shift across \0 path
        # delimiters (PRRT_kwDOSJAM6s6eRK93).
        file_hasher = hashlib.sha256()
        file_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        file_hasher.update(b"\0")
        candidate = byte_root / path
        try:
            kind_info = _worktree_entry_kind(candidate)
            if kind_info is None:
                try:
                    candidate.lstat()
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        file_hasher.update(b"<missing>")
                    else:
                        return None
                else:
                    return None
            else:
                entry_digest = _digest_worktree_entry_bytes(
                    worktree_path=worktree_path,
                    path=path,
                    git_env=env,
                )
                if entry_digest is None:
                    return None
                file_hasher.update(entry_digest)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                file_hasher.update(b"<missing>")
            else:
                # Unreadable residue (e.g. mode 000) must fail closed: hashing a
                # shared <missing> marker collides across different contents when
                # the commit sink also cannot stage the file (PRRT_kwDOSJAM6s6eN7wf).
                return None
        untracked_hasher.update(file_hasher.digest())
    return untracked_hasher.hexdigest()


def _nested_untrusted_git_probe_timed_out_result(
    command: list[str],
    *,
    stderr: bytes,
) -> subprocess.CompletedProcess[bytes]:
    """
    Create a completed-process result representing a timed-out nested Git probe.
    
    Parameters:
    	command (list[str]): The Git command that was interrupted.
    	stderr (bytes): Diagnostic output associated with the timeout.
    
    Returns:
    	subprocess.CompletedProcess[bytes]: A result with return code 124 and empty standard output.
    """
    return subprocess.CompletedProcess(
        args=command,
        returncode=124,
        stdout=b"",
        stderr=stderr,
    )


def _list_nested_nul_git_path_records(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    args: tuple[str, ...],
) -> tuple[bytes, ...] | None:
    """List nested Git path records using NUL-delimited output with configured record, size, and time limits.
    
    Parameters:
        worktree_path (Path): Path to the nested worktree.
        git_env (Mapping[str, str]): Environment variables for the Git command.
        args (tuple[str, ...]): Arguments passed to Git.
    
    Returns:
        tuple[bytes, ...] | None: The path records, or `None` if the command fails or exceeds a limit.
    """
    command = _git_command_for_residue_probe(worktree_path, *args)
    env = dict(git_env)
    # Snapshot git-dir already embeds objects/refs; do not re-pin live common dir
    # (would reload mutable common ``config`` — PRRT_kwDOSJAM6s6elv_p).
    if _NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.get() is None:
        pinned_common = _fresh_pinned_nested_git_common_dir()
        if pinned_common is not None:
            env["GIT_COMMON_DIR"] = str(pinned_common)
    timeout = _residue_git_probe_command_timeout()
    if timeout is None and _NESTED_FINGERPRINT_SCAN_ACTIVE.get():
        timeout = _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    return _popen_capped_nul_path_records(
        command,
        env=env,
        max_records=_NESTED_UNTRACKED_LS_FILES_MAX_PATHS,
        max_bytes=_NESTED_UNTRACKED_LS_FILES_MAX_STDOUT_BYTES,
        timeout=timeout,
    )


def _list_nested_untracked_paths_capped(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> set[str] | None:
    """
    Collect untracked paths in a nested worktree under configured enumeration limits.
    
    Parameters:
    	worktree_path (Path): Path to the nested worktree.
    	git_env (Mapping[str, str]): Environment variables for the Git command.
    
    Returns:
    	set[str] | None: The untracked paths, including ignored entries, or `None` if enumeration fails or exceeds a configured limit.
    """
    records = _list_nested_nul_git_path_records(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("ls-files", "-o", "-z"),
    )
    if records is None:
        return None
    paths: set[str] = set()
    for part in records:
        if not _directory_enum_consume_entries(1):
            return None
        paths.add(part.decode("utf-8", errors="surrogateescape"))
    return paths


def _list_nested_tracked_changed_paths_capped(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    cached: bool,
) -> tuple[str, ...] | None:
    """
    List changed tracked paths in the nested worktree with bounded enumeration.
    
    Parameters:
        cached (bool): Whether to list paths from the staged index state.
    
    Returns:
        tuple[str, ...] | None: Unique changed paths, or `None` if enumeration
        fails or exceeds its limits.
    """
    records = _list_nested_nul_git_path_records(
        worktree_path=worktree_path,
        git_env=git_env,
        args=_tracked_residue_changed_paths_args(cached=cached),
    )
    if records is None:
        return None
    paths: list[str] = []
    seen: set[str] = set()
    for part in records:
        if not _directory_enum_consume_entries(1):
            return None
        path = part.decode("utf-8", errors="surrogateescape")
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _run_git_bytes(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    args: tuple[str, ...],
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """
    Execute a Git command for a worktree with bounded output capture and timeout handling.
    
    Parameters:
        worktree_path (Path): Worktree on which to run the command.
        args (tuple[str, ...]): Git command arguments.
        stdin (bytes | None): Optional input supplied to the command.
    
    Returns:
        subprocess.CompletedProcess[bytes]: Captured command result, including a synthesized timeout result when the probe exceeds its budget.
    """
    command = _git_command_for_residue_probe(worktree_path, *args)
    env = dict(git_env)
    # Sanitized nested envs strip GIT_COMMON_DIR; re-pin from the retained approved
    # common-dir fd so Git does not re-read a mutable marker ``commondir``
    # (PRRT_kwDOSJAM6s6ecAB2). Skip when a validated config snapshot git-dir is
    # active — live common config must not be reloaded (PRRT_kwDOSJAM6s6elv_p).
    if _NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.get() is None:
        pinned_common = _fresh_pinned_nested_git_common_dir()
        if pinned_common is not None:
            env["GIT_COMMON_DIR"] = str(pinned_common)
    timeout = _residue_git_probe_command_timeout()
    if timeout == 0.0:
        return _nested_untrusted_git_probe_timed_out_result(
            command,
            stderr=b"nested untrusted git probe scan budget exceeded",
        )
    try:
        return subprocess.run(
            command,
            env=env,
            capture_output=True,
            check=False,
            input=stdin,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _nested_untrusted_git_probe_timed_out_result(
            command,
            stderr=b"nested untrusted git probe timed out",
        )


def _git_index_blob_sha(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Retrieve the index blob ID for a worktree path.
    
    Parameters:
    	worktree_path (Path): Path to the Git worktree.
    	path (str): Path whose indexed blob ID should be retrieved.
    	git_env (Mapping[str, str]): Environment variables for the Git command.
    
    Returns:
    	str | None: The indexed blob ID, or `None` if the path has no resolvable index entry.
    """
    result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        # ``:{path}`` is ambiguous when ``path`` begins with ``0:``–``3:`` (Git's
        # ``:<stage>:<path>`` index syntax); ``:0:./`` disambiguates (PRRT_kwDOSJAM6s6eQcs6).
        args=("rev-parse", "-q", "--verify", f":0:./{path}"),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


def _git_worktree_blob_sha(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
    index_mode: str | None = None,
) -> str | None:
    """
    Compute the Git blob identifier for a worktree entry.
    
    Parameters:
    	worktree_path (Path): Root path of the worktree.
    	path (str): Path to the entry relative to the worktree.
    	git_env (Mapping[str, str]): Environment used for Git operations.
    	index_mode (str | None): Optional index mode used to identify directory submodules.
    
    Returns:
    	str | None: The Git object identifier for the entry, or `None` if the entry is missing, unreadable, invalid, or cannot be hashed.
    """
    byte_root = _worktree_root_for_residue_byte_reads(worktree_path)
    candidate = byte_root / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, st_mode = kind_info

    if kind == "symlink":
        try:
            # Component-wise no-follow parent walk + dir_fd readlink
            # (PRRT_kwDOSJAM6s6eiJk-); pathname readlink follows mid-path swaps.
            blob_bytes = _read_worktree_symlink_under_root(
                byte_root,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            )
        except OSError:
            return None
        result = _run_git_bytes(
            worktree_path=worktree_path,
            git_env=git_env,
            # ``hash-object --path`` invokes path clean filters and can block or hang
            # (PRRT_kwDOSJAM6s6eSHjC); hash raw worktree bytes via stdin instead.
            args=("hash-object", "--stdin"),
            stdin=blob_bytes,
        )
    elif kind == "regular":
        try:
            with _open_worktree_regular_file_under_root(
                byte_root,
                path,
                root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
            ) as fh:
                # Bounded revalidated snapshot + GIT_COMMON_DIR pin + component
                # no-follow (PRRT_kwDOSJAM6s6eSPQL / eeAsG / ef8Fg / ef8Fm).
                # Live ``stdin_stream`` hangs when an outer appender never EOF's
                # and nested-probe timeouts are inactive.
                snapshot = _read_opened_regular_file_snapshot(fh)
                if snapshot is None:
                    return None
                result = _run_git_bytes(
                    worktree_path=worktree_path,
                    git_env=git_env,
                    args=("hash-object", "--stdin"),
                    stdin=snapshot,
                )
        except OSError:
            return None
    elif kind == "directory":
        if index_mode == "160000" or _has_nested_git_marker(candidate):
            return _git_nested_worktree_commit(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
        return _hash_worktree_directory_residue(
            worktree_path=worktree_path,
            path=path,
            git_env=git_env,
        )
    elif kind in _SPECIAL_ENTRY_KINDS:
        return _special_entry_blob_sha(kind=kind, st_mode=st_mode)
    else:  # pragma: no cover - kinds always come from _worktree_entry_kind_from_mode
        return None

    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


@contextlib.contextmanager
def _pinned_nested_git_dir_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[bool]:
    """Yield True when nested git-dir probes are pinned to an opened marker or gitfile."""
    with _open_nested_git_dir_marker_at(
        dir_fd,
        outer_worktree_path=outer_worktree_path,
    ) as opened:
        if opened is not None:
            marker_fd, common_fd = opened
            marker_token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(marker_fd)
            common_token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(common_fd)
            try:
                yield True
            finally:
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_token)
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(marker_token)
            return
        with _open_nested_git_dir_gitfile_target_at(
            dir_fd,
            outer_worktree_path=outer_worktree_path,
        ) as opened_gitfile:
            if opened_gitfile is None:
                yield False
                return
            gitfile_target_fd, common_fd = opened_gitfile
            token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.set(gitfile_target_fd)
            common_token = _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.set(common_fd)
            try:
                yield True
            finally:
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_COMMON_FD.reset(common_token)
                _NESTED_UNTRUSTED_GIT_PROBE_GIT_MARKER_FD.reset(token)


def _nested_git_probe_worktree_root(
    *,
    nested_root: Path,
    git_env: Mapping[str, str],
) -> Path | None:
    """
    Determine the effective worktree root reported by Git for a nested repository.
    
    Parameters:
    	nested_root (Path): Root directory of the nested repository.
    
    Returns:
    	Path | None: Resolved worktree root, or `None` if Git cannot report a valid root.
    """
    result = _run_git_bytes(
        worktree_path=nested_root,
        git_env=git_env,
        args=("rev-parse", "--show-toplevel"),
    )
    if result.returncode != 0:
        return None
    reported = result.stdout.decode("utf-8", errors="surrogateescape").strip()
    if not reported:
        return None
    try:
        return Path(reported).resolve()
    except OSError:
        return None


def _git_nested_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Compute the identity of a nested Git repository.
    
    Parameters:
        worktree_path (Path): Outer worktree containing the nested repository.
        path (str): Repository path relative to the worktree.
    
    Returns:
        str | None: The repository identity, or `None` if it cannot be determined.
    """
    try:
        with _open_worktree_directory(
            worktree_path,
            path,
            root_dir_fd=_NESTED_UNTRUSTED_GIT_PROBE_WORKTREE_FD.get(),
        ) as dir_fd:
            return _git_nested_worktree_commit_at(
                dir_fd=dir_fd,
                git_env=git_env,
                outer_worktree_path=_fresh_pinned_nested_worktree() or worktree_path,
            )
    except OSError:
        return None


def _git_nested_worktree_commit_at(
    *,
    dir_fd: int,
    git_env: Mapping[str, str],
    outer_worktree_path: Path,
) -> str | None:
    """Return nested Git identity for a pinned directory fd without pathname re-entry."""
    if not _has_nested_git_marker_at(dir_fd):
        return None
    if _fresh_worktree_path_for_open_fd(dir_fd) is None:
        return None
    return _git_nested_worktree_commit_from_root(
        dir_fd=dir_fd,
        git_env=git_env,
        outer_worktree_path=outer_worktree_path,
    )


def _resolve_nested_worktree_head(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> str | None:
    """
    Resolve the nested repository's HEAD state.
    
    Parameters:
    	worktree_path (Path): Path to the nested repository worktree.
    	git_env (Mapping[str, str]): Environment variables for Git commands.
    
    Returns:
    	str | None: The HEAD commit ID, ``<unborn>`` when HEAD points to an
    	unborn branch, or ``None`` when the HEAD state cannot be resolved safely.
    """
    head_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("rev-parse", "HEAD"),
    )
    if head_result.returncode == 0:
        head = head_result.stdout.decode("ascii", errors="replace").strip()
        return head if head else None

    verify_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("rev-parse", "--verify", "HEAD^{commit}"),
    )
    if verify_result.returncode == 0:
        # HEAD resolves as a commit but ``rev-parse HEAD`` failed — fail closed.
        return None

    symref_result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("symbolic-ref", "-q", "HEAD"),
    )
    if symref_result.returncode != 0:
        return None
    if not symref_result.stdout.strip():
        return None
    return _UNBORN_HEAD_SENTINEL


def _git_nested_worktree_commit_from_root(
    *,
    dir_fd: int,
    git_env: Mapping[str, str],
    outer_worktree_path: Path,
) -> str | None:
    """Compute a nested repository fingerprint from its HEAD and tracked and untracked residue.
    
    Parameters:
    	dir_fd (int): File descriptor for the nested repository's Git metadata.
    	git_env (Mapping[str, str]): Environment variables used for Git operations.
    	outer_worktree_path (Path): Approved outer worktree containing the nested repository.
    
    Returns:
    	str | None: SHA-256 fingerprint of the nested repository state, or `None` if the repository cannot be safely or completely inspected.
    """
    nested_git_env = git_env_for_untrusted_nested_repository_probe(git_env)
    containment_roots = _approved_git_metadata_roots(outer_worktree_path)
    with _untrusted_nested_git_probe():
        if _nested_untrusted_git_probe_past_deadline():
            return None
        with _without_nested_git_probe_pin():
            nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
            if nested_root is None:
                return None
            # ``-c`` overrides do not stop Git from loading local include.path /
            # includeIf files (PRRT_kwDOSJAM6s6ekfTU); reject before any probe.
            # Do not run Git against the live nested root here: a surviving agent
            # can inject include.path → FIFO after this check and block
            # ``rev-parse`` until the nested-probe timeout (PRRT_kwDOSJAM6s6ewpcq).
            # Discovery waits for the validated config snapshot below.
            if untrusted_nested_repository_local_config_has_includes(
                nested_root,
                containment_roots=containment_roots,
            ):
                return None

            # Snapshot must enter via the retained descriptor, not a mutable
            # readlink pathname: a rename + decoy between refresh and open would
            # otherwise materialize the decoy's config/objects (PRRT_kwDOSJAM6s6eqQgs).
            snapshot_root = _worktree_proc_path_for_open_fd(dir_fd)
            if snapshot_root is None:
                return None

        # Freeze validated local config into a private git-dir for the rest of the
        # probe lifetime so a surviving agent cannot inject includes mid-flight
        # (PRRT_kwDOSJAM6s6elv_p). Materialization also re-checks includes.
        with untrusted_nested_probe_config_snapshot_git_dir(
            snapshot_root,
            containment_roots=containment_roots,
        ) as snapshot_git_dir:
            if snapshot_git_dir is None:
                return None
            with _nested_probe_config_snapshot_git_dir(snapshot_git_dir):
                with _without_nested_git_probe_pin():
                    # Git rejects bare ``/proc/self/fd/<fd>`` for ``-C``; refresh the
                    # inode's current pathname immediately before discovery.
                    # Discovery uses the snapshotted git-dir (not live config).
                    nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
                    if nested_root is None:
                        return None
                    probe_root = _nested_git_probe_worktree_root(
                        nested_root=nested_root,
                        git_env=nested_git_env,
                    )
                    if probe_root is None:
                        return None
                    # Reject agent-redirected worktrees outside the AWF checkout before open
                    # (PRRT_kwDOSJAM6s6eadgA); in-checkout redirects remain allowed.
                    if not _nested_probe_root_within_outer_worktree(
                        probe_root=probe_root,
                        worktree_path=outer_worktree_path,
                    ):
                        return None

                with _open_worktree_directory_path(
                    probe_root,
                    outer_worktree_path=outer_worktree_path,
                ) as probe_worktree_fd:
                    if probe_worktree_fd is None:
                        return None
                    with (
                        _pinned_nested_worktree_fd(probe_worktree_fd),
                        _pinned_nested_git_dir_at(
                            dir_fd,
                            outer_worktree_path=outer_worktree_path,
                        ) as has_pinned_git_dir,
                    ):
                        if not has_pinned_git_dir:
                            return None
                        git_dir = _fresh_pinned_nested_git_dir()
                        pinned_worktree = _fresh_pinned_nested_worktree()
                        if git_dir is None or pinned_worktree is None:
                            return None
                        with _pinned_nested_git_probe(git_dir, pinned_worktree):
                            worktree_path = _fresh_pinned_nested_worktree()
                            if worktree_path is None:
                                return None
                            head = _resolve_nested_worktree_head(
                                worktree_path=worktree_path,
                                git_env=nested_git_env,
                            )
                            if head is None:
                                return None

                            worktree_path = _fresh_pinned_nested_worktree()
                            if worktree_path is None:
                                return None
                            inner_staged, inner_unstaged = (
                                _hash_tracked_residue_staged_and_unstaged(
                                    worktree_path=worktree_path,
                                    git_env=nested_git_env,
                                )
                            )
                            if inner_staged is None or inner_unstaged is None:
                                return None

                            worktree_path = _fresh_pinned_nested_worktree()
                            if worktree_path is None:
                                return None
                            # Cap streamed ``ls-files -o`` so path floods cannot OOM
                            # (PRRT_kwDOSJAM6s6efXeI).
                            untracked = _list_nested_untracked_paths_capped(
                                worktree_path=worktree_path,
                                git_env=nested_git_env,
                            )
                            if untracked is None:
                                return None
                            if untracked:
                                worktree_path = _fresh_pinned_nested_worktree()
                                if worktree_path is None:
                                    return None
                                inner_untracked = _hash_untracked_residue_paths(
                                    worktree_path=worktree_path,
                                    paths=sorted(untracked),
                                    untracked=untracked,
                                    git_env=nested_git_env,
                                )
                                if inner_untracked is None:
                                    return None
                            else:
                                inner_untracked = hashlib.sha256().hexdigest()

    hasher = hashlib.sha256()
    hasher.update(b"head:")
    hasher.update(head.encode("ascii"))
    hasher.update(b"\0staged:")
    hasher.update(inner_staged.encode("ascii"))
    hasher.update(b"\0unstaged:")
    hasher.update(inner_unstaged.encode("ascii"))
    hasher.update(b"\0untracked:")
    hasher.update(inner_untracked.encode("ascii"))
    return hasher.hexdigest()


def _git_submodule_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """
    Compute the identity of a tracked submodule worktree.
    
    The identity includes the checked-out commit and staged, unstaged, and untracked
    residue within the submodule. Returns no identity when the submodule lacks a
    valid ``.git`` marker.
    
    Parameters:
        path (str): Path of the tracked submodule within the worktree.
    
    Returns:
        str | None: The submodule worktree identity, or ``None`` when it cannot be
        established.
    """
    return _git_nested_worktree_commit(
        worktree_path=worktree_path,
        path=path,
        git_env=git_env,
    )


def _git_worktree_mode(
    *,
    worktree_path: Path,
    path: str,
) -> str | None:
    """Determine the Git tree mode for a worktree entry.
    
    Parameters:
        worktree_path (Path): Root directory of the worktree.
        path (str): Relative path of the entry within the worktree.
    
    Returns:
        str | None: Git tree mode for the entry, or `None` if the entry is missing or unsupported.
    """
    candidate = worktree_path / path
    kind_info = _worktree_entry_kind(candidate)
    if kind_info is None:
        return None
    kind, file_mode = kind_info
    if kind == "symlink":
        return "120000"
    if kind == "regular":
        if stat.S_IMODE(file_mode) & stat.S_IXUSR:
            return "100755"
        return "100644"
    if kind == "directory":
        return "040000"
    if kind in _SPECIAL_ENTRY_KINDS:
        return kind
    return None  # pragma: no cover - kinds always come from _worktree_entry_kind_from_mode


def _tracked_residue_changed_paths_args(*, cached: bool) -> tuple[str, ...]:
    """
    Select Git arguments for listing changed paths while preserving submodule changes.
    
    Parameters:
        cached (bool): Whether to list staged changes from the index.
    
    Returns:
        tuple[str, ...]: Git arguments for listing changed paths in NUL-delimited form.
    """
    if cached:
        return ("diff", "--cached", "--name-only", "-z", "--ignore-submodules=none")
    if _NESTED_UNTRUSTED_GIT_PROBE.get():
        # ``git diff --name-only`` runs committed .gitattributes clean filters on
        # worktree bytes; ``git diff-files`` compares index to worktree without them
        # (PRRT_kwDOSJAM6s6eWICC). Still pass ``--ignore-submodules=none``: per-submodule
        # ``submodule.<name>.ignore`` overrides ``-c diff.ignoreSubmodules=none``
        # (PRRT_kwDOSJAM6s6ehEtb).
        return ("diff-files", "--name-only", "-z", "--ignore-submodules=none")
    return ("diff", "--name-only", "-z", "--ignore-submodules=none")
