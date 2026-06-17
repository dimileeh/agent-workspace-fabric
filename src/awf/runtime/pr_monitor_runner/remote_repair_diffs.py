"""Remote-branch changed-path diff helpers for the PR monitor runner.

Split out of ``awf.runtime.pr_monitor_runner.remote_repair`` to keep that module
under the first-party line limit. These methods resolve the committed diff of a
worktree against its remote PR branch and classify protected-scope changes; they
are attached to ``PullRequestMonitorRunner`` via the runner mixin and keep
behavior unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from awf.common.commands import CommandResult
from awf.common.git_identity import (
    git_safe_directory_config_args,
)
from awf.control.protected_file_diffs import (
    git_show_text,
)
from awf.control.quality_gates import (
    ProtectedFileDiff,
    diff_classified_protected_paths,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_name_status_z,
    _read_worktree_text,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)


async def _changed_paths_since_remote_branch(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
) -> tuple[str, ...]:
    _, changed_paths = await self._remote_branch_diff_base_and_changed_paths(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    return cast(tuple[str, ...], changed_paths)


async def _remote_branch_diff_base_and_changed_paths(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    remote_push_url: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    remote = remote_push_url or "origin"
    fetch_result = await self._remote_branch_fetch_once(
        worktree_path=worktree_path,
        remote=remote,
        remote_branch=remote_branch,
    )
    repairs_attempted = 0
    while not fetch_result.ok and repairs_attempted < _GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS:
        try:
            repaired = await self._repair_orphaned_broken_awf_ref(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                stderr=fetch_result.stderr,
            )
        except Exception as exc:
            raise ProtectedScopeDiffError(
                "Could not resolve committed diff against the remote PR branch "
                "for protected-scope validation: broken AWF ref repair failed "
                f"after fetch failure: {exc!r}"
            ) from exc
        if not repaired:
            break
        repairs_attempted += 1
        fetch_result = await self._remote_branch_fetch_once(
            worktree_path=worktree_path,
            remote=remote,
            remote_branch=remote_branch,
        )
    if not fetch_result.ok:
        raise ProtectedScopeDiffError(
            "Could not resolve committed diff against the remote PR branch "
            "for protected-scope validation: "
            f"fetch refs/heads/{remote_branch} exit={fetch_result.returncode} "
            f"stdout={fetch_result.stdout.strip() or '<empty>'} "
            f"stderr={fetch_result.stderr.strip() or '<empty>'}"
        )
    local_base = await self._merge_base_with_head(
        worktree_path=worktree_path,
        ref="FETCH_HEAD",
        error_context="against the remote PR branch",
    )
    changed_paths = await self._changed_paths_between_ref_and_head(
        worktree_path=worktree_path,
        ref=local_base,
        error_context="against the remote PR branch",
    )
    return local_base, changed_paths


async def _remote_branch_fetch_once(
    self: Any,
    *,
    worktree_path: Path,
    remote: str,
    remote_branch: str,
) -> CommandResult:
    result: CommandResult = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        ]
    )
    return result


async def _merge_base_with_head(
    self: Any,
    *,
    worktree_path: Path,
    ref: str,
    error_context: str,
) -> str:
    merge_base_result = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "merge-base",
            ref,
            "HEAD",
        ]
    )
    merge_base = merge_base_result.stdout.strip()
    if not merge_base_result.ok or not merge_base:
        raise ProtectedScopeDiffError(
            f"Could not resolve committed diff {error_context} "
            "for protected-scope validation: "
            f"merge-base {ref} HEAD exit={merge_base_result.returncode} "
            f"stdout={merge_base or '<empty>'} "
            f"stderr={merge_base_result.stderr.strip() or '<empty>'}"
        )
    return cast(str, merge_base)


async def _changed_paths_between_ref_and_head(
    self: Any,
    *,
    worktree_path: Path,
    ref: str,
    error_context: str,
) -> tuple[str, ...]:
    diff_spec = f"{ref}..HEAD"
    diff_result = await self._deps.runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "diff",
            "--name-status",
            "-z",
            diff_spec,
            "--",
        ]
    )
    if not diff_result.ok:
        raise ProtectedScopeDiffError(
            f"Could not resolve committed diff {error_context} "
            "for protected-scope validation: "
            f"diff {diff_spec} exit={diff_result.returncode} "
            f"stdout={diff_result.stdout.strip() or '<empty>'} "
            f"stderr={diff_result.stderr.strip() or '<empty>'}"
        )
    try:
        return _changed_paths_from_name_status_z(diff_result.stdout)
    except ProtectedScopeDiffError as exc:
        raise ProtectedScopeDiffError(
            f"Could not parse committed diff {error_context} for protected-scope validation: {exc}"
        ) from exc


async def _protected_file_diffs_for_status_paths(
    self: Any,
    *,
    worktree_path: Path,
    changed_paths: Sequence[str],
    owned_paths: Sequence[str] = (),
) -> dict[str, ProtectedFileDiff]:
    diffs: dict[str, ProtectedFileDiff] = {}
    for path in diff_classified_protected_paths(changed_paths, owned_paths=owned_paths):
        worktree_file = worktree_path / path
        old_text = await git_show_text(
            self._deps.runner, worktree_path=worktree_path, refspec=f"HEAD:{path}"
        )
        new_text = (
            _read_worktree_text(worktree_file, display_path=path)
            if worktree_file.exists()
            else None
        )
        diffs[path] = ProtectedFileDiff(
            path=path,
            old_text=old_text,
            new_text=new_text,
        )
    return diffs
