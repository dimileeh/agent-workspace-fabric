"""Pre-commit autofix retry helpers for PR monitor repair commits."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.git_utils import (
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_porcelain,
    _split_porcelain_rename_paths,
    _unquote_porcelain_path,
    _untracked_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.precommit_autofix import (
    monitor_precommit_autofix_repair_paths,
)
from awf.runtime.validation_worktree import (
    is_under_agent_runtime_root,
)


def _worktree_modified_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract paths with unstaged worktree changes from porcelain output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line or len(line) < 4 or line[2] != " ":
            continue
        status = line[:2]
        if status == "??" or status[1] == " ":
            continue
        path = line[3:]
        rename_paths = (
            _split_porcelain_rename_paths(path)
            if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}
            else None
        )
        if rename_paths:
            _old_path, path = rename_paths
        paths.append(_unquote_porcelain_path(path))
    return list(dict.fromkeys(paths))


def _path_in_operation_dirty_scope(
    path: str,
    operation_dirty_path_set: set[str],
    operation_dirty_dir_prefixes: Sequence[str],
) -> bool:
    if path in operation_dirty_path_set:
        return True
    return any(path.startswith(prefix) for prefix in operation_dirty_dir_prefixes)


def _monitor_precommit_autofix_repair_paths(commit_result: CommandResult) -> tuple[str, ...]:
    """Thin shim kept for targeted test-import isolation; do not add logic here."""
    return monitor_precommit_autofix_repair_paths(commit_result)


async def _retry_monitor_precommit_autofix_commit_once(
    *,
    runner: Any,
    workspace_id: str,
    worktree_path: Path,
    message: str,
    commit_result: CommandResult,
    operation_dirty_paths: Sequence[str],
) -> tuple[CommandResult, tuple[str, ...]] | None:
    repair_paths = _monitor_precommit_autofix_repair_paths(commit_result)
    if not repair_paths:
        return None

    # ``--untracked-files=all`` is load-bearing, exactly as in the commit path's
    # stage-status read: with git's default ``normal`` mode a fully-untracked
    # ``.claude/agent-memory/`` collapses to a single ``?? .claude/`` entry whose
    # parent is *not* under the agent-runtime root, so it escapes
    # ``is_under_agent_runtime_root`` and is wrongly counted as dirt outside the
    # operation scope — aborting the retry. Enumerating leaf paths lets the same
    # filter drop the memory files, leaving only the hook-autofixed in-scope
    # changes to evaluate. The commit path already scoped ``operation_dirty_paths``
    # to the filtered ``stage_paths``; this keeps the retry's own status read in
    # lockstep with it.
    dirty_status = await runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all")
    )
    if not dirty_status.ok:
        _log.warning(
            "monitor.dirty_commit_autofix_status_failed",
            workspace_id=workspace_id,
            stderr=dirty_status.stderr[:400],
        )
        return None

    dirty_untracked = set(_untracked_paths_from_porcelain(dirty_status.stdout))
    dirty_paths = tuple(
        path
        for path in _changed_paths_from_porcelain(dirty_status.stdout)
        if not (path in dirty_untracked and is_under_agent_runtime_root(path))
    )
    if not dirty_paths:
        _log.info(
            "monitor.dirty_commit_autofix_retry_skipped_clean",
            workspace_id=workspace_id,
            repair_paths=list(repair_paths),
        )
        return None

    dirty_path_set = set(dirty_paths)
    repair_path_set = set(repair_paths)
    operation_dirty_path_set = set(operation_dirty_paths)
    operation_dirty_dir_prefixes = tuple(
        path for path in operation_dirty_path_set if path.endswith("/")
    )
    dirty_paths_outside_operation = {
        path
        for path in dirty_path_set
        if not _path_in_operation_dirty_scope(
            path,
            operation_dirty_path_set,
            operation_dirty_dir_prefixes,
        )
    }
    worktree_modified_paths = tuple(_worktree_modified_paths_from_porcelain(dirty_status.stdout))
    worktree_modified_path_set = set(worktree_modified_paths)
    if dirty_paths_outside_operation or not worktree_modified_path_set <= repair_path_set:
        _log.warning(
            "monitor.dirty_commit_autofix_retry_skipped_unsafe",
            workspace_id=workspace_id,
            dirty_paths=sorted(dirty_path_set),
            dirty_paths_outside_operation=sorted(dirty_paths_outside_operation),
            repair_paths=sorted(repair_path_set),
            operation_dirty_paths=sorted(operation_dirty_path_set),
            worktree_modified_paths=sorted(worktree_modified_path_set),
        )
        return None

    restage_paths = tuple(path for path in worktree_modified_paths if path in repair_path_set)
    if not restage_paths:
        _log.info(
            "monitor.dirty_commit_autofix_retry_skipped_no_repair_paths",
            workspace_id=workspace_id,
            dirty_paths=list(dirty_paths),
            repair_paths=list(repair_paths),
        )
        return None

    add = await runner.run(git_worktree_command(worktree_path, "add", "--", *restage_paths))
    if not add.ok:
        _log.warning(
            "monitor.dirty_commit_autofix_add_failed",
            workspace_id=workspace_id,
            paths=list(restage_paths),
            stderr=add.stderr[:400],
        )
        return None

    retry = await runner.run(git_worktree_command(worktree_path, "commit", "-m", message))
    return retry, restage_paths
