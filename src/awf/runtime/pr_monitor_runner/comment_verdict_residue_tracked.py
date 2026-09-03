"""Tracked residue index/diff hashing for correction fingerprints.

Kept separate so ``comment_verdict_residue`` stays under the first-party line budget.
Callers and tests continue to use these helpers via re-exports on
``comment_verdict_residue`` so monkeypatches on the facade still apply.
"""

from __future__ import annotations

import errno
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError


def _residue() -> ModuleType:
    """Lazy facade import so monkeypatches on ``comment_verdict_residue`` apply."""
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as residue

    return residue


def _git_index_mode(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Retrieve the staged Git index mode for a path.
    
    Parameters:
        worktree_path (Path): Path to the Git worktree.
        path (str): Repository-relative path to inspect.
        git_env (Mapping[str, str]): Environment variables for the Git command.
    
    Returns:
        str | None: The staged index mode, or `None` if the query fails or no index entry exists.
    """
    result = _residue()._run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("ls-files", "--stage", "-z", "--", path),
    )
    if result.returncode != 0:
        return None
    first_entry = result.stdout.split(b"\0", 1)[0]
    if not first_entry:
        return None
    mode = first_entry.split(b" ", 1)[0]
    return mode.decode("ascii", errors="replace") or None


def _parse_git_index_stage_records(
    raw: bytes | tuple[bytes, ...],
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Parse ``ls-files --stage -z`` records into path -> ((stage, mode, blob), ...).

    Retains every index stage so conflicted paths fingerprint stage-1/2/3 mutations
    rather than collapsing to the last non-zero stage (PRRT_kwDOSJAM6s6ewJZn).
    Stages are sorted numerically for stable hashing; duplicate stage keys last-win.
    """
    records: tuple[bytes, ...]
    if isinstance(raw, bytes):
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts = parts[:-1]
        records = tuple(parts)
    else:
        records = raw
    collected: dict[str, dict[str, tuple[str, str, str]]] = {}
    for entry in records:
        try:
            meta, path_b = entry.split(b"\t", 1)
        except ValueError:
            continue
        meta_parts = meta.split(b" ")
        if len(meta_parts) < 3:
            continue
        mode_b, blob_b, stage_b = meta_parts[0], meta_parts[1], meta_parts[2]
        path = path_b.decode("utf-8", errors="surrogateescape")
        mode = mode_b.decode("ascii", errors="replace")
        blob = blob_b.decode("ascii", errors="replace")
        stage = stage_b.decode("ascii", errors="replace")
        if not path or not mode or not blob or not stage:
            continue
        collected.setdefault(path, {})[stage] = (stage, mode, blob)
    parsed: dict[str, tuple[tuple[str, str, str], ...]] = {}
    for path, by_stage in collected.items():
        parsed[path] = tuple(
            sorted(
                by_stage.values(),
                key=lambda item: int(item[0]) if item[0].isdigit() else 99,
            )
        )
    return parsed


def _representative_index_stage(
    stages: tuple[tuple[str, str, str], ...],
) -> tuple[str, str]:
    """
    Select the index stage used to represent a path.
    
    Parameters:
        stages (tuple[tuple[str, str, str], ...]): Available index stages as
            ``(stage, mode, blob)`` tuples.
    
    Returns:
        tuple[str, str]: The selected stage's ``(mode, blob)`` pair, preferring
            stage 0 and otherwise using the lowest available stage.
    
    Raises:
        ValueError: If ``stages`` is empty.
    """
    if not stages:  # pragma: no cover - parse never yields empty stage tuples
        raise ValueError("stages must not be empty")
    for stage, mode, blob in stages:
        if stage == "0":
            return mode, blob
    return stages[0][1], stages[0][2]


class _BytesHasher(Protocol):
    def update(self, data: bytes, /) -> None: ...  # pragma: no cover - Protocol stub


def _hash_index_stage_entries(
    hasher: _BytesHasher,
    stage_entries: tuple[tuple[str, str, str], ...] | None,
    *,
    missing_blob: str,
) -> None:
    """
    Add a tracked path's Git index stage identity to a hash.
    
    Parameters:
        hasher: Hash object to update.
        stage_entries: Index entries represented as stage, mode, and blob tuples, or
            ``None`` when the path has no index entry.
        missing_blob: Blob identity to use when the index entry is missing.
    """
    if stage_entries is None:
        hasher.update(b"index:")
        hasher.update(missing_blob.encode("ascii"))
        hasher.update(b"im:")
        hasher.update(b"<missing>")
        return
    if len(stage_entries) == 1 and stage_entries[0][0] == "0":
        _, mode, blob = stage_entries[0]
        hasher.update(b"index:")
        hasher.update(blob.encode("ascii"))
        hasher.update(b"im:")
        hasher.update(mode.encode("ascii"))
        return
    for stage, mode, blob in stage_entries:
        hasher.update(b"s:")
        hasher.update(stage.encode("ascii"))
        hasher.update(b"index:")
        hasher.update(blob.encode("ascii"))
        hasher.update(b"im:")
        hasher.update(mode.encode("ascii"))


def _load_git_index_stage_map(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    paths: Sequence[str],
) -> dict[str, tuple[tuple[str, str, str], ...]] | None:
    """
    Load staged mode and blob identifiers for the specified paths from the Git index.
    
    Parameters:
        worktree_path (Path): Path to the Git worktree.
        git_env (Mapping[str, str]): Environment variables for the Git probe.
        paths (Sequence[str]): Paths whose index records should be loaded.
    
    Returns:
        dict[str, tuple[tuple[str, str, str], ...]] | None: A mapping from paths to
        their index stage, mode, and blob records, or ``None`` if the probe fails
        or exceeds its deadline.
    """
    if not paths:
        return {}
    if (
        _residue()._nested_untrusted_git_probe_past_deadline()
        or _residue()._ordinary_fingerprint_git_past_deadline()
    ):
        return None
    env = dict(git_env)
    if _residue()._NESTED_UNTRUSTED_GIT_PROBE_CONFIG_SNAPSHOT_GIT_DIR.get() is None:
        pinned_common = _residue()._fresh_pinned_nested_git_common_dir()
        if pinned_common is not None:
            env["GIT_COMMON_DIR"] = str(pinned_common)
    timeout = _residue()._residue_git_probe_command_timeout()
    if timeout is None:
        timeout = _residue()._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS
    parsed: dict[str, tuple[tuple[str, str, str], ...]] = {}
    for offset in range(0, len(paths), _residue()._INDEX_STAGE_LS_FILES_PATH_CHUNK):
        if (
            _residue()._nested_untrusted_git_probe_past_deadline()
            or _residue()._ordinary_fingerprint_git_past_deadline()
        ):
            return None
        chunk = tuple(paths[offset : offset + _residue()._INDEX_STAGE_LS_FILES_PATH_CHUNK])
        command = _residue()._git_command_for_residue_probe(
            worktree_path,
            "--literal-pathspecs",
            "ls-files",
            "--stage",
            "-z",
            "--",
            *chunk,
        )
        # Conflicted paths may emit up to three stage records.
        max_records = min(
            _residue()._NESTED_UNTRACKED_LS_FILES_MAX_PATHS,
            max(len(chunk) * 3, 1),
        )
        records = _residue()._popen_capped_nul_path_records(
            command,
            env=env,
            max_records=max_records,
            max_bytes=_residue()._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES,
            timeout=timeout,
        )
        if records is None:
            return None
        parsed.update(_residue()._parse_git_index_stage_records(records))
    return parsed


def _hash_tracked_residue_diffs(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    cached: bool,
) -> str | None:
    """
    Compute a SHA-256 fingerprint of tracked changes without creating patch data.
    
    Parameters:
    	worktree_path (Path): Root path of the Git worktree.
    	git_env (Mapping[str, str]): Environment variables used for Git commands.
    	cached (bool): Whether to fingerprint staged index content instead of worktree content.
    
    Returns:
    	str | None: The fingerprint, or `None` if the change data cannot be determined.
    """
    if (
        _residue()._NESTED_UNTRUSTED_GIT_PROBE.get()
        or _residue()._NESTED_FINGERPRINT_SCAN_ACTIVE.get()
    ):
        paths = _residue()._list_nested_tracked_changed_paths_capped(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=cached,
        )
        if paths is None:
            return None
    else:
        diff_args = _residue()._tracked_residue_changed_paths_args(cached=cached)
        name_result = _residue()._run_git_bytes(
            worktree_path=worktree_path, git_env=git_env, args=diff_args
        )
        if name_result.returncode != 0:
            return None
        try:
            paths = _residue()._changed_paths_from_name_only_z(name_result.stdout)
        except ProtectedScopeDiffError:
            return None

    hasher = hashlib.sha256()
    if not paths:
        return hasher.hexdigest()
    if (
        _residue()._nested_untrusted_git_probe_past_deadline()
        or _residue()._ordinary_fingerprint_git_past_deadline()
    ):
        return None
    index_stages = _residue()._load_git_index_stage_map(
        worktree_path=worktree_path,
        git_env=git_env,
        paths=paths,
    )
    if index_stages is None:
        return None
    for path in sorted(paths):
        if (
            _residue()._nested_untrusted_git_probe_past_deadline()
            or _residue()._ordinary_fingerprint_git_past_deadline()
        ):
            return None
        hasher.update(path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        stage_entries = index_stages.get(path)
        if stage_entries is None:
            index_mode = None
            index_blob = None
        else:
            index_mode, index_blob = _residue()._representative_index_stage(stage_entries)
        if cached:
            _residue()._hash_index_stage_entries(
                hasher,
                stage_entries,
                missing_blob="<missing>",
            )
        else:
            worktree_blob = _residue()._git_worktree_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
                index_mode=index_mode,
            )
            if worktree_blob is None:
                candidate = worktree_path / path
                if index_blob is not None:
                    try:
                        candidate.lstat()
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            # Ordinary tracked deletions are absent from the worktree but
                            # still indexed; ``hash-object --path`` returns None without
                            # being unreadable (PRRT_kwDOSJAM6s6eP-gA).
                            worktree_blob = "<deleted>"
                        else:
                            # ``Path.exists()`` also returns False on permission and other
                            # stat errors; those must fail closed, not hash ``<deleted>``
                            # (Bugbot review 5082437263).
                            return None
                    else:
                        if index_mode == "160000":
                            # Gitlinks are directories; fingerprint checked-out submodule HEAD
                            # instead of failing closed (PRRT_kwDOSJAM6s6eRyfx).
                            worktree_blob = _residue()._git_submodule_worktree_commit(
                                worktree_path=worktree_path,
                                path=path,
                                git_env=git_env,
                            )
                            if worktree_blob is None:
                                return None
                        else:
                            # Worktree path is present but ``hash-object`` failed — unreadable.
                            return None
                else:
                    return None
            worktree_mode = _residue()._git_worktree_mode(
                worktree_path=worktree_path,
                path=path,
            )
            if worktree_mode is None and index_mode == "160000":
                worktree_mode = "160000"
            _residue()._hash_index_stage_entries(
                hasher,
                stage_entries,
                missing_blob="<none>",
            )
            hasher.update(b"wt:")
            hasher.update(worktree_blob.encode("ascii"))
            hasher.update(b"wm:")
            hasher.update((worktree_mode or "<missing>").encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_tracked_residue_staged_and_unstaged(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """
    Compute separate fingerprints for tracked staged and unstaged changes.
    
    Parameters:
    	worktree_path (Path): Path to the Git worktree.
    	git_env (Mapping[str, str]): Environment variables used for Git commands.
    
    Returns:
    	tuple[str | None, str | None]: Staged and unstaged fingerprints, respectively. Each value is `None` if the corresponding fingerprint cannot be computed.
    """
    return (
        _residue()._hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=True,
        ),
        _residue()._hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=False,
        ),
    )
