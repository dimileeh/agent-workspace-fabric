"""Correction-attempt residue fingerprint helpers for verdict protocol retries."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def _hash_untracked_residue_paths(
    *,
    worktree_path: Path,
    paths: list[str],
    untracked: set[str],
) -> str:
    """Sync content identity for untracked PR-worthy paths.

    Intended for ``asyncio.to_thread`` so multi-gigabyte non-ignored artifacts
    do not block the monitor event loop (PRRT_kwDOSJAM6s6eLMRD). Symlinks are
    fingerprinted via link text only — never followed (PRRT_kwDOSJAM6s6eK9AB).
    """
    untracked_hasher = hashlib.sha256()
    for path in paths:
        if path not in untracked:
            continue
        untracked_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        untracked_hasher.update(b"\0")
        candidate = worktree_path / path
        try:
            if candidate.is_symlink():
                link_text = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
                untracked_hasher.update(b"symlink:")
                untracked_hasher.update(link_text)
            else:
                with candidate.open("rb") as fh:
                    while chunk := fh.read(65536):
                        untracked_hasher.update(chunk)
        except OSError:
            untracked_hasher.update(b"<missing>")
        untracked_hasher.update(b"\0")
    return untracked_hasher.hexdigest()


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

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    status_lines = sorted(
        line
        for line in (status.stdout or "").splitlines()
        if line
        and any(candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n"))
    )

    async def _diff_probe(*args: str) -> str | None:
        try:
            result = await runner._deps.runner.run(
                git_worktree_command(worktree_path, *args),
                env=git_env,
            )
        except Exception as exc:
            _log.warning(
                "monitor.agent_verdict_correction_residue_diff_failed",
                workspace_id=workspace_id,
                diff_args=list(args),
                exc_type=type(exc).__name__,
                error=str(exc)[:400],
            )
            return None
        if not result.ok:
            _log.warning(
                "monitor.agent_verdict_correction_residue_diff_failed",
                workspace_id=workspace_id,
                diff_args=list(args),
                returncode=result.returncode,
                stderr=(result.stderr or "")[:400],
            )
            return None
        return result.stdout or ""

    staged = await _diff_probe("diff", "--cached")
    if staged is None:
        return None
    unstaged = await _diff_probe("diff")
    if unstaged is None:
        return None

    untracked_digest = await asyncio.to_thread(
        _hash_untracked_residue_paths,
        worktree_path=worktree_path,
        paths=paths,
        untracked=untracked,
    )

    return "\n".join(
        [
            *status_lines,
            f"staged:{_sha256_utf8(staged)}",
            f"unstaged:{_sha256_utf8(unstaged)}",
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
