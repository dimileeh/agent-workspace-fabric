"""Pre-commit autofix retry helpers for PR monitor repair commits."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.comments import (
    _git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.precommit_autofix import (
    monitor_precommit_autofix_repair_paths,
)


def _worktree_modified_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract paths with unstaged worktree changes from porcelain output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " " and line[1] != " "):
            path = line[3:]
        else:
            continue
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.extend([old_path, new_path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _monitor_precommit_autofix_repair_paths(commit_result: CommandResult) -> tuple[str, ...]:
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

    dirty_status = await runner.run(_git_worktree_command(worktree_path, "status", "--porcelain"))
    if not dirty_status.ok:
        _log.warning(
            "monitor.dirty_commit_autofix_status_failed",
            workspace_id=workspace_id,
            stderr=dirty_status.stderr[:400],
        )
        return None

    dirty_paths = tuple(_changed_paths_from_porcelain(dirty_status.stdout))
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
    worktree_modified_paths = tuple(_worktree_modified_paths_from_porcelain(dirty_status.stdout))
    worktree_modified_path_set = set(worktree_modified_paths)
    if (
        not dirty_path_set <= operation_dirty_path_set
        or not worktree_modified_path_set <= repair_path_set
    ):
        _log.warning(
            "monitor.dirty_commit_autofix_retry_skipped_unsafe",
            workspace_id=workspace_id,
            dirty_paths=sorted(dirty_path_set),
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

    add = await runner.run(_git_worktree_command(worktree_path, "add", "--", *restage_paths))
    if not add.ok:
        _log.warning(
            "monitor.dirty_commit_autofix_add_failed",
            workspace_id=workspace_id,
            paths=list(restage_paths),
            stderr=add.stderr[:400],
        )
        return None

    retry = await runner.run(_git_worktree_command(worktree_path, "commit", "-m", message))
    return retry, restage_paths
