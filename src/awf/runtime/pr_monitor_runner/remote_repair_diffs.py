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
from awf.db.repositories import WorkspaceEventCreate
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.constants import (
    _GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _GIT_BASE_FETCH_TRANSIENT_RETRY_REASON,
    _GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _base_fetch_retry_wait_seconds,
    _changed_paths_from_name_status_z,
    _is_transient_base_fetch_error,
    _read_worktree_text,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeBaselineUnavailableError,
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
            _log.exception(
                "monitor.git_mirror_broken_ref_repair_failed",
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                repairs_attempted=repairs_attempted,
            )
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
    fetch_result = await self._retry_transient_remote_branch_fetch(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote=remote,
        remote_branch=remote_branch,
        fetch_result=fetch_result,
        context="protected_scope_diff_baseline",
    )
    if not fetch_result.ok:
        raise _remote_branch_fetch_error_type(fetch_result)(
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


def _is_transient_remote_branch_fetch_failure(fetch_result: CommandResult) -> bool:
    """Classify a failed PR-branch fetch with the base-fetch transient classifier."""
    return not fetch_result.ok and _is_transient_base_fetch_error(
        BaseFetchError(f"{fetch_result.stderr}\n{fetch_result.stdout}")
    )


def _remote_branch_fetch_error_type(
    fetch_result: CommandResult,
) -> type[ProtectedScopeDiffError]:
    """Pick the fail-closed error for a PR-branch fetch that is still failing.

    A transient failure that outlived its retry budget raises the narrower
    ``ProtectedScopeBaselineUnavailableError`` so the push seams park the workspace
    instead of failing it (#1221); anything else keeps the terminal error.
    """
    if _is_transient_remote_branch_fetch_failure(fetch_result):
        return ProtectedScopeBaselineUnavailableError
    return ProtectedScopeDiffError


async def _retry_transient_remote_branch_fetch(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote: str,
    remote_branch: str,
    fetch_result: CommandResult,
    context: str,
) -> CommandResult:
    """Retry a transiently failed PR-branch fetch with the base-fetch backoff (#1221).

    Reuses the base-fetch classifier and the ``transient_base_fetch_*`` budget, so
    DNS, connection reset/timeout, TLS and forge 5xx blips get the same bounded
    retry as ``monitor.git_base_fetch_transient_retrying``. The counter is local to
    this call: each baseline computation is its own bounded episode. Returns the
    last fetch result; a non-transient failure returns immediately, unchanged.
    """
    max_retries = max(self._runner_config.transient_base_fetch_max_retries, 0)
    retry_number = 0
    while _is_transient_remote_branch_fetch_failure(fetch_result):
        exhausted = retry_number >= max_retries
        wait_seconds = 0.0
        event_type = "monitor.protected_scope_baseline_fetch_transient_retry_exhausted"
        reason_code = _GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON
        if not exhausted:
            retry_number += 1
            wait_seconds = _base_fetch_retry_wait_seconds(
                retry_number=retry_number,
                initial_backoff_seconds=(
                    self._runner_config.transient_base_fetch_initial_backoff_seconds
                ),
                max_backoff_seconds=self._runner_config.transient_base_fetch_max_backoff_seconds,
            )
            event_type = "monitor.protected_scope_baseline_fetch_transient_retrying"
            reason_code = _GIT_BASE_FETCH_TRANSIENT_RETRY_REASON
        payload: dict[str, object] = {
            "context": context,
            "operation": "git fetch remote branch",
            "remote_branch": remote_branch,
            "retry_number": retry_number,
            "max_retries": max_retries,
            "wait_seconds": wait_seconds,
            "message": _redact_and_truncate_forge_error(
                fetch_result.stderr.strip() or fetch_result.stdout.strip()
            ),
        }
        _log.warning(event_type, workspace_id=workspace_id, reason_code=reason_code, **payload)
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type=event_type,
                    reason_code=reason_code,
                    payload=payload,
                )
            ],
        )
        if exhausted:
            break
        await self._deps.sleep(wait_seconds)
        fetch_result = await self._remote_branch_fetch_once(
            worktree_path=worktree_path,
            remote=remote,
            remote_branch=remote_branch,
        )
    return fetch_result


async def _remote_branch_fetch_once(
    self: Any,
    *,
    worktree_path: Path,
    remote: str,
    remote_branch: str,
    timeout_seconds: float | None = None,
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
        ],
        timeout_seconds=timeout_seconds,
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
        ],
        env=git_env_without_object_lookup_overrides(),
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
