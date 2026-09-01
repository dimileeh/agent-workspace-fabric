"""Correction-attempt residue fingerprint helpers for verdict protocol retries."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_helpers import _changed_paths_from_name_only_z
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


def _hash_untracked_residue_paths(
    *,
    worktree_path: Path,
    paths: list[str],
    untracked: set[str],
) -> str | None:
    """Sync content identity for untracked PR-worthy paths.

    Intended for ``asyncio.to_thread`` so multi-gigabyte non-ignored artifacts
    do not block the monitor event loop (PRRT_kwDOSJAM6s6eLMRD). Symlinks are
    fingerprinted via link text only — never followed (PRRT_kwDOSJAM6s6eK9AB).
    """
    untracked_hasher = hashlib.sha256()
    for path in paths:
        if path not in untracked:
            continue
        # Hash each file independently so raw bytes cannot shift across \0 path
        # delimiters (PRRT_kwDOSJAM6s6eRK93).
        file_hasher = hashlib.sha256()
        file_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        file_hasher.update(b"\0")
        candidate = worktree_path / path
        try:
            if candidate.is_symlink():
                link_text = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
                file_hasher.update(b"symlink:")
                worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
                file_hasher.update(b"mode:")
                file_hasher.update((worktree_mode or "<missing>").encode("ascii"))
                file_hasher.update(b"\0")
                file_hasher.update(link_text)
            else:
                file_hasher.update(b"regular:")
                worktree_mode = _git_worktree_mode(worktree_path=worktree_path, path=path)
                file_hasher.update(b"mode:")
                file_hasher.update((worktree_mode or "<missing>").encode("ascii"))
                file_hasher.update(b"\0")
                with candidate.open("rb") as fh:
                    while chunk := fh.read(65536):
                        file_hasher.update(chunk)
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


def _run_git_bytes(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    args: tuple[str, ...],
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        git_worktree_command(worktree_path, *args),
        env=dict(git_env),
        capture_output=True,
        check=False,
        input=stdin,
    )


def _git_index_blob_sha(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
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
) -> str | None:
    candidate = worktree_path / path
    try:
        if candidate.is_symlink():
            # ``hash-object --path`` opens the worktree path and follows symlinks;
            # fingerprint link text via stdin instead (Bugbot review 5081034196).
            link_bytes = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
            result = _run_git_bytes(
                worktree_path=worktree_path,
                git_env=git_env,
                args=("hash-object", "--stdin"),
                stdin=link_bytes,
            )
            if result.returncode != 0:
                return None
            return result.stdout.decode("ascii", errors="replace").strip() or None
    except OSError:
        return None
    result = _run_git_bytes(
        worktree_path=worktree_path,
        git_env=git_env,
        args=("hash-object", "--path", path, "--", path),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


def _git_submodule_worktree_commit(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    """Return the checked-out commit at a tracked gitlink (submodule) path."""
    submodule_root = worktree_path / path
    result = _run_git_bytes(
        worktree_path=submodule_root,
        git_env=git_env,
        args=("rev-parse", "HEAD"),
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii", errors="replace").strip() or None


def _git_index_mode(
    *,
    worktree_path: Path,
    path: str,
    git_env: Mapping[str, str],
) -> str | None:
    result = _run_git_bytes(
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


def _git_worktree_mode(
    *,
    worktree_path: Path,
    path: str,
) -> str | None:
    candidate = worktree_path / path
    try:
        file_mode = candidate.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISLNK(file_mode):
        return "120000"
    if stat.S_ISREG(file_mode):
        if stat.S_IMODE(file_mode) & stat.S_IXUSR:
            return "100755"
        return "100644"
    return None


def _hash_tracked_residue_diffs(
    *,
    worktree_path: Path,
    git_env: Mapping[str, str],
    cached: bool,
) -> str | None:
    """Hash tracked change identity without materializing full ``git diff`` patches.

    ``git diff --name-only -z`` bounds stdout to path names; per-path blob SHAs
    come from ``rev-parse :path`` / ``hash-object --path`` so multi-gigabyte edits
    cannot exhaust the control-plane process (PRRT_kwDOSJAM6s6eM1NH).
    """
    diff_args = (
        ("diff", "--cached", "--name-only", "-z") if cached else ("diff", "--name-only", "-z")
    )
    name_result = _run_git_bytes(worktree_path=worktree_path, git_env=git_env, args=diff_args)
    if name_result.returncode != 0:
        return None
    try:
        paths = _changed_paths_from_name_only_z(name_result.stdout)
    except ProtectedScopeDiffError:
        return None

    hasher = hashlib.sha256()
    for path in sorted(paths):
        hasher.update(path.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        if cached:
            index_blob = _git_index_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            index_mode = _git_index_mode(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            hasher.update(b"index:")
            hasher.update((index_blob or "<missing>").encode("ascii"))
            hasher.update(b"im:")
            hasher.update((index_mode or "<missing>").encode("ascii"))
        else:
            index_blob = _git_index_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            index_mode = _git_index_mode(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
            )
            worktree_blob = _git_worktree_blob_sha(
                worktree_path=worktree_path,
                path=path,
                git_env=git_env,
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
                            worktree_blob = _git_submodule_worktree_commit(
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
            worktree_mode = _git_worktree_mode(
                worktree_path=worktree_path,
                path=path,
            )
            if worktree_mode is None and index_mode == "160000":
                worktree_mode = "160000"
            hasher.update(b"index:")
            hasher.update((index_blob or "<none>").encode("ascii"))
            hasher.update(b"im:")
            hasher.update((index_mode or "<missing>").encode("ascii"))
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
    return (
        _hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=True,
        ),
        _hash_tracked_residue_diffs(
            worktree_path=worktree_path,
            git_env=git_env,
            cached=False,
        ),
    )


async def _read_correction_pr_worthy_residue_fingerprint(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Return a fingerprint of PR-worthy dirty porcelain.

    Empty string means clean. ``None`` means the status probe failed and callers
    must fail closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.

    Path names alone are not enough: when attempt 0 leaves ``src/x.py`` dirty and
    the correction edits that same file, a path-only fingerprint collides and
    attribution treats the mutation as pre-existing residue
    (PRRT_kwDOSJAM6s6eKj9D). Include staged/unstaged diff hashes and untracked
    file content identity while retaining the runtime-path exclusion.
    """
    if not worktree_path.exists():
        return ""

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner.path_parsing import (
        _changed_paths_from_porcelain,
        _untracked_paths_from_porcelain,
    )
    from awf.runtime.validation_worktree import is_under_agent_runtime_root

    git_env = git_env_without_object_lookup_overrides()

    try:
        status = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ),
            env=git_env,
        )
    except Exception as exc:
        # Spawn failures (e.g. OSError from create_subprocess_exec) must fail
        # closed like a non-ok status so the correction mutation path rolls back
        # unaccepted dirty edits (PRRT_kwDOSJAM6s6eJi5X).
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        return None
    if not status.ok:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            returncode=status.returncode,
            stderr=(status.stderr or "")[:400],
        )
        return None
    if not (status.stdout or "").strip():
        return ""
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    paths = sorted(
        path
        for path in _changed_paths_from_porcelain(status.stdout)
        if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not paths:
        return ""

    tracked_paths = [path for path in paths if path not in untracked]

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    status_lines = sorted(
        line
        for line in (status.stdout or "").splitlines()
        if line
        and any(candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n"))
    )

    try:
        if tracked_paths:
            staged_digest, unstaged_digest = await asyncio.to_thread(
                _hash_tracked_residue_staged_and_unstaged,
                worktree_path=worktree_path,
                git_env=git_env,
            )
        else:
            empty_digest = hashlib.sha256().hexdigest()
            staged_digest = unstaged_digest = empty_digest
    except Exception as exc:
        _log.warning(
            "monitor.agent_verdict_correction_residue_diff_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        return None
    if staged_digest is None or unstaged_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_diff_failed",
            workspace_id=workspace_id,
            staged_digest=staged_digest,
            unstaged_digest=unstaged_digest,
        )
        return None

    untracked_digest = await asyncio.to_thread(
        _hash_untracked_residue_paths,
        worktree_path=worktree_path,
        paths=paths,
        untracked=untracked,
    )
    if untracked_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_untracked_unreadable",
            workspace_id=workspace_id,
        )
        return None

    return "\n".join(
        [
            *status_lines,
            f"staged:{staged_digest}",
            f"unstaged:{unstaged_digest}",
            f"untracked:{untracked_digest}",
        ]
    )


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
        # Unreadable baseline: any pre-sink dirt cannot be proven pre-existing.
        return bool(pre_sink_residue_fp)
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
        return bool(post_residue_fp)
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
    return bool(fingerprint)
