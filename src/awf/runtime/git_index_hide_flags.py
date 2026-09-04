"""Detect and clear Git index hide flags (assume-unchanged / skip-worktree).

``git update-index --assume-unchanged`` / ``--skip-worktree`` omit tracked edits
from ``git status`` even when forced ``core.*`` overrides are applied. Correction
residue fingerprints and validation cleanliness checks must clear those bits
before porcelain status so hidden mutations cannot collide with a clean baseline
(review 5109730762 / PRRT_kwDOSJAM6s6fLsRy). Pre-clear flag snapshots are not
embedded in correction fingerprints: clearing is the monitor's own mutation and
must not diverge consecutive start/end identities (PRRT_kwDOSJAM6s6fNhZo).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path

from awf.common.commands import CommandResult
from awf.common.git_identity import git_safe_directory_config_args

# ``git ls-files -v`` tags that hide worktree edits from ordinary status.
# ``h`` = assume-unchanged; ``S`` = skip-worktree; ``s`` = both.
_HIDE_FLAG_TAGS = frozenset({"h", "S", "s"})
_UPDATE_INDEX_PATH_CHUNK = 1024
_GIT_TIMEOUT_SECONDS = 30.0
# Same byte scale as ordinary residue Git stdout caps.
_LS_FILES_V_MAX_STDOUT_BYTES = 32 * 1024 * 1024

GitRunner = Callable[..., Awaitable[CommandResult]]


def _git_command(worktree_path: Path, *args: str) -> list[str]:
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        *args,
    ]


def parse_ls_files_v_hide_entries(
    records: Sequence[bytes] | bytes | str,
) -> list[tuple[str, str]]:
    """Return ``(tag, path)`` entries for hide-flag tags from ``ls-files -v`` output.

    Accepts NUL-delimited record tuples/bytes (``-z``) or newline-delimited text.
    """
    raw_records: tuple[bytes, ...]
    if isinstance(records, str):
        text_parts = records.split("\0") if "\0" in records else records.splitlines()
        raw_records = tuple(
            part.encode("utf-8", errors="surrogateescape") for part in text_parts if part
        )
    elif isinstance(records, bytes):
        byte_parts = records.split(b"\0")
        if byte_parts and byte_parts[-1] == b"":
            byte_parts = byte_parts[:-1]
        raw_records = tuple(part for part in byte_parts if part)
    else:
        raw_records = tuple(records)

    entries: list[tuple[str, str]] = []
    for record in raw_records:
        if len(record) < 3 or record[1:2] != b" ":
            continue
        tag = record[:1].decode("ascii", errors="replace")
        if tag not in _HIDE_FLAG_TAGS:
            continue
        path = record[2:].decode("utf-8", errors="surrogateescape")
        if not path:
            continue
        entries.append((tag, path))
    entries.sort(key=lambda item: (item[1], item[0]))
    return entries


def parse_index_hide_flags_snapshot(snapshot: str) -> list[tuple[str, str]]:
    """Parse canonical ``tag path`` snapshot lines produced by ``format_index_hide_flags_snapshot``."""
    entries: list[tuple[str, str]] = []
    for line in snapshot.splitlines():
        if len(line) < 3 or line[1] != " ":
            continue
        tag, path = line[0], line[2:]
        if tag in _HIDE_FLAG_TAGS and path:
            entries.append((tag, path))
    entries.sort(key=lambda item: (item[1], item[0]))
    return entries


def format_index_hide_flags_snapshot(entries: Sequence[tuple[str, str]]) -> str:
    """Canonical text for fingerprinting hide-flag identity."""
    return "".join(f"{tag} {path}\n" for tag, path in entries)


def hash_index_hide_flags_snapshot(snapshot: str) -> str:
    """SHA-256 hex digest of a hide-flag snapshot."""
    return hashlib.sha256(snapshot.encode("utf-8", errors="surrogateescape")).hexdigest()


def _paths_for_hide_flag_clear(
    entries: Sequence[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Split hide entries into assume-unchanged vs skip-worktree path lists."""
    assume_paths: list[str] = []
    skip_paths: list[str] = []
    seen_assume: set[str] = set()
    seen_skip: set[str] = set()
    for tag, path in entries:
        if tag in ("h", "s") and path not in seen_assume:
            assume_paths.append(path)
            seen_assume.add(path)
        if tag in ("S", "s") and path not in seen_skip:
            skip_paths.append(path)
            seen_skip.add(path)
    return assume_paths, skip_paths


def snapshot_index_hide_flags(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> str | None:
    """Return canonical hide-flag snapshot text, or ``None`` to fail closed.

    Empty string means no hide flags.
    """
    command = _git_command(
        worktree_path,
        "--literal-pathspecs",
        "ls-files",
        "-v",
        "-z",
    )
    try:
        result = subprocess.run(
            command,
            env=dict(git_env),
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if len(result.stdout) > _LS_FILES_V_MAX_STDOUT_BYTES:
        return None
    return format_index_hide_flags_snapshot(parse_ls_files_v_hide_entries(result.stdout))


def _run_update_index_clear(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    flag_arg: str,
    paths: Sequence[str],
) -> bool:
    if not paths:
        return True
    for offset in range(0, len(paths), _UPDATE_INDEX_PATH_CHUNK):
        chunk = paths[offset : offset + _UPDATE_INDEX_PATH_CHUNK]
        command = _git_command(
            worktree_path,
            "--literal-pathspecs",
            "update-index",
            flag_arg,
            "--",
            *chunk,
        )
        try:
            result = subprocess.run(
                command,
                env=dict(git_env),
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def clear_index_hide_flags(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    snapshot: str | None = None,
) -> bool:
    """Clear assume-unchanged / skip-worktree bits (separate update-index calls).

    When ``snapshot`` is provided it must be the pre-clear canonical text from
    ``snapshot_index_hide_flags`` so callers can fingerprint before mutating.
    """
    if snapshot is None:
        snapshot = snapshot_index_hide_flags(worktree_path=worktree_path, git_env=git_env)
        if snapshot is None:
            return False
    entries = parse_index_hide_flags_snapshot(snapshot)
    assume_paths, skip_paths = _paths_for_hide_flag_clear(entries)
    if not _run_update_index_clear(
        worktree_path=worktree_path,
        git_env=git_env,
        flag_arg="--no-assume-unchanged",
        paths=assume_paths,
    ):
        return False
    return _run_update_index_clear(
        worktree_path=worktree_path,
        git_env=git_env,
        flag_arg="--no-skip-worktree",
        paths=skip_paths,
    )


def snapshot_and_clear_index_hide_flags(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> str | None:
    """Snapshot hide flags then clear them. Returns snapshot or ``None`` fail-closed."""
    snapshot = snapshot_index_hide_flags(worktree_path=worktree_path, git_env=git_env)
    if snapshot is None:
        return None
    if not clear_index_hide_flags(
        worktree_path=worktree_path,
        git_env=git_env,
        snapshot=snapshot,
    ):
        return None
    return snapshot


async def clear_index_hide_flags_via_run_git(run_git: GitRunner) -> bool:
    """Clear hide flags through an async validation ``run_git`` callback.

    Callers that need a finite timeout (validation worktree cleanliness) should
    wrap ``run_git`` with their timeout injector (for example
    ``_run_validation_git``) before passing it here.
    """
    listed = await run_git(["--literal-pathspecs", "ls-files", "-v", "-z"])
    if not listed.ok:
        return False
    stdout_bytes = getattr(listed, "stdout_bytes", None)
    if stdout_bytes is not None:
        raw: bytes | str = stdout_bytes
    else:
        raw = listed.stdout or ""
    entries = parse_ls_files_v_hide_entries(raw)
    assume_paths, skip_paths = _paths_for_hide_flag_clear(entries)
    for flag_arg, paths in (
        ("--no-assume-unchanged", assume_paths),
        ("--no-skip-worktree", skip_paths),
    ):
        for offset in range(0, len(paths), _UPDATE_INDEX_PATH_CHUNK):
            chunk = paths[offset : offset + _UPDATE_INDEX_PATH_CHUNK]
            result = await run_git(
                ["--literal-pathspecs", "update-index", flag_arg, "--", *chunk],
            )
            if not result.ok:
                return False
    return True
