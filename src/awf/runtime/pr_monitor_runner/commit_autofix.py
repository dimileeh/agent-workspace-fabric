"""Pre-commit autofix retry helpers for PR monitor repair commits."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awf.common.commands import CommandResult
from awf.control.executor.quality_gates import (
    _classify_post_agent_commit_failure,
)
from awf.runtime.pr_monitor_runner.comments import (
    _git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.logging import _log

_PRE_COMMIT_AUTOFIX_MARKER = "files were modified by this hook"


def _monitor_precommit_autofix_repair_paths(commit_result: CommandResult) -> tuple[str, ...]:
    output = f"{commit_result.stdout or ''}\n{commit_result.stderr or ''}"
    if _PRE_COMMIT_AUTOFIX_MARKER not in output:
        return ()

    classification = _classify_post_agent_commit_failure(commit_result)
    if classification.repair_strategy != "deterministic":
        return ()

    return tuple(
        dict.fromkeys(
            (
                *classification.normalizer_repair_files,
                *classification.format_repair_files,
                *classification.autofix_repair_files,
            )
        )
    )


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
    if not dirty_path_set <= repair_path_set or not dirty_path_set <= operation_dirty_path_set:
        _log.warning(
            "monitor.dirty_commit_autofix_retry_skipped_unsafe",
            workspace_id=workspace_id,
            dirty_paths=sorted(dirty_path_set),
            repair_paths=sorted(repair_path_set),
            operation_dirty_paths=sorted(operation_dirty_path_set),
        )
        return None

    add = await runner.run(_git_worktree_command(worktree_path, "add", "--", *dirty_paths))
    if not add.ok:
        _log.warning(
            "monitor.dirty_commit_autofix_add_failed",
            workspace_id=workspace_id,
            paths=list(dirty_paths),
            stderr=add.stderr[:400],
        )
        return None

    retry = await runner.run(_git_worktree_command(worktree_path, "commit", "-m", message))
    return retry, dirty_paths
