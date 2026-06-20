"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from awf.common.commands import CommandResult
from awf.common.git_identity import (
    git_identity_config_args,
    git_safe_directory_config_args,
)
from awf.common.task_tag import commit_message_with_task_tag
from awf.control.protected_file_diffs import protected_file_diffs_for_committed_paths
from awf.control.quality_gates import (
    QualityGateViolation,
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
)
from awf.db.repositories import (
    MergeCandidateRepository,
    WorkspaceRepository,
)
from awf.node.git_manager import (
    GitOperationError,
    git_env_without_object_lookup_overrides,
    mirror_path_for_worktree,
    repair_agent_writable_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _retry_monitor_precommit_autofix_commit_once,
)
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_RECOVERED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
    _REPAIR_START_HEAD_UNAVAILABLE_REASON,
    _REPAIR_WORKTREE_STATUS_FAILED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_porcelain,
    _untracked_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_name_status_z,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree import (
    is_under_agent_runtime_root,
)


async def _pre_existing_dirty_repair_worktree_result(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_type: str,
) -> _GitPushResult | None:
    if not worktree_path.exists():
        return None
    # ``--untracked-files=all`` is load-bearing here: with git's default
    # ``normal`` mode a *fully*-untracked ``.claude/`` (no tracked content under
    # it) collapses all the way to a single ``?? .claude/`` entry, which is NOT
    # under the ``.claude/agent-memory/`` ignored root and would therefore stay
    # in ``paths`` and refuse repair in the common case this guard unblocks.
    # Enumerating leaf paths lets the agent-runtime filter below see and drop the
    # memory files. Mirrors ``check_validation_worktree_clean``.
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all"),
        env=git_env_without_object_lookup_overrides(),
    )
    if not status.ok:
        stderr = status.stderr[:400]
        _log.warning(
            "monitor.repair_worktree_status_failed",
            workspace_id=workspace_id,
            operation_type=operation_type,
            returncode=status.returncode,
            stderr=stderr,
        )
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=status.returncode,
            stderr="Could not inspect repair worktree before starting the agent.",
            reason_code=_REPAIR_WORKTREE_STATUS_FAILED_REASON,
            details={
                "phase": "repair_start",
                "operation_type": operation_type,
                "status_stderr": stderr,
                "pushed": False,
            },
        )
    if not status.stdout.strip():
        return None

    # AWF-agent-runtime artifacts (reviewer subagent memory) written into the
    # repair worktree are not part of the PR, so drop UNTRACKED memory paths
    # before deciding the worktree is dirty. Tracked-modified memory (and every
    # other path) stays visible/blocking. If nothing else remains, the worktree
    # is effectively clean — return None, same as the empty-status path above.
    all_paths = _changed_paths_from_porcelain(status.stdout)
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    paths = sorted(
        path for path in all_paths if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not paths:
        return None
    _log.warning(
        "monitor.repair_worktree_pre_existing_dirty",
        workspace_id=workspace_id,
        operation_type=operation_type,
        paths=paths,
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "Repair worktree has pre-existing uncommitted changes; "
            "refusing to start agent repair because protected-scope rollback "
            "would not be limited to the current operation."
        ),
        reason_code=_PRE_EXISTING_DIRTY_WORKTREE_REASON,
        details={
            "phase": "repair_start",
            "operation_type": operation_type,
            "paths": paths,
            "pushed": False,
        },
    )


async def _repair_operation_start_head_result(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_type: str,
    fallback_head_sha: str | None = None,
    allow_candidate_fallback: bool = True,
) -> tuple[str, _GitPushResult | None]:
    def failure_result(
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        fallback_head: str | None = None,
        fallback_source: str | None = None,
    ) -> _GitPushResult:
        details: dict[str, object] = {
            "phase": "repair_start",
            "operation_type": operation_type,
            "head_stdout": stdout,
            "head_stderr": stderr,
            "pushed": False,
        }
        if fallback_head is not None:
            details["fallback_head_sha"] = fallback_head
        if fallback_source is not None:
            details["fallback_source"] = fallback_source
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=returncode if returncode != 0 else 1,
            stderr=(
                "Could not capture repair operation start HEAD before starting the agent; "
                "refusing to start repair because protected-scope rollback would not have "
                "a stable baseline."
            ),
            reason_code=_REPAIR_START_HEAD_UNAVAILABLE_REASON,
            details=details,
        )

    async def validated_fallback_result(
        fallback_head: str,
        *,
        source: str,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> tuple[str, _GitPushResult | None]:
        mirror_path = mirror_path_for_worktree(worktree_path)
        if mirror_path is not None:
            fallback_exists = await _mirror_commit_object_exists(self, mirror_path, fallback_head)
        elif worktree_path.exists():
            fallback_exists = await _worktree_commit_object_exists(
                self, worktree_path, fallback_head
            )
        else:
            fallback_exists = await verify_head_object_exists(worktree_path)
        if not fallback_exists:
            _log.warning(
                "monitor.repair_operation_start_head_fallback_unavailable",
                workspace_id=workspace_id,
                operation_type=operation_type,
                head_sha=fallback_head[:10],
                source=source,
                mirror_path=str(mirror_path) if mirror_path is not None else None,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
            return "", failure_result(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                fallback_head=fallback_head,
                fallback_source=source,
            )
        _log.info(
            "monitor.repair_operation_start_head_from_fallback",
            workspace_id=workspace_id,
            operation_type=operation_type,
            head_sha=fallback_head[:10],
            source=source,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        return fallback_head, None

    async def start_head_fallback() -> tuple[str | None, str]:
        if fallback_head_sha:
            return fallback_head_sha, "status"
        if not allow_candidate_fallback:
            return None, "candidate"
        return await self._open_merge_candidate_head_sha(workspace_id), "candidate"

    if not worktree_path.exists():
        fallback_head, source = await start_head_fallback()
        if fallback_head:
            return await validated_fallback_result(
                fallback_head,
                source=source,
                returncode=1,
                stdout="",
                stderr="repair worktree is missing",
            )
    result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", "HEAD"),
        env=git_env_without_object_lookup_overrides(),
    )
    head_sha = result.stdout.strip()
    if result.ok and head_sha:
        mirror_path = mirror_path_for_worktree(worktree_path)
        if mirror_path is not None:
            head_exists = await _mirror_commit_object_exists(self, mirror_path, head_sha)
        else:
            head_exists = await _worktree_commit_object_exists(self, worktree_path, head_sha)
        if not head_exists:
            stdout = result.stdout[:400]
            stderr = result.stderr[:400]
            _log.warning(
                "monitor.repair_operation_start_head_primary_unavailable",
                workspace_id=workspace_id,
                operation_type=operation_type,
                head_sha=head_sha[:10],
                mirror_path=str(mirror_path) if mirror_path is not None else None,
                returncode=result.returncode,
                stdout=stdout,
                stderr=stderr,
            )
            fallback_head, source = await start_head_fallback()
            if fallback_head:
                return await validated_fallback_result(
                    fallback_head,
                    source=source,
                    returncode=result.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
            return "", failure_result(
                returncode=result.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return head_sha, None

    stdout = result.stdout[:400]
    stderr = result.stderr[:400]
    fallback_head, source = await start_head_fallback()
    if fallback_head:
        return await validated_fallback_result(
            fallback_head,
            source=source,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    _log.warning(
        "monitor.repair_operation_start_head_unavailable",
        workspace_id=workspace_id,
        operation_type=operation_type,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return "", failure_result(
        returncode=result.returncode if result.returncode != 0 else 1,
        stdout=stdout,
        stderr=stderr,
    )


async def _open_merge_candidate_head_sha(self: Any, workspace_id: str) -> str | None:
    async with self._deps.session_factory() as session:
        repository = MergeCandidateRepository(session)
        candidate = await repository.get_open_for_workspace_with_merge_inputs(workspace_id)
        return candidate.head_sha if candidate is not None else None


async def _mirror_commit_object_exists(self: Any, mirror_path: Path, commit_sha: str) -> bool:
    result = cast(
        CommandResult,
        await self._deps.runner.run(
            ["git", "--git-dir", str(mirror_path), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
            env=git_env_without_object_lookup_overrides(),
        ),
    )
    return result.ok


async def _worktree_commit_object_exists(self: Any, worktree_path: Path, commit_sha: str) -> bool:
    result = cast(
        CommandResult,
        await self._deps.runner.run(
            git_worktree_command(worktree_path, "cat-file", "-e", f"{commit_sha}^{{commit}}"),
            env=git_env_without_object_lookup_overrides(),
        ),
    )
    return result.ok


async def _protected_scope_violations_for_recovered_dirty_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_ref: str,
    changed_paths: Sequence[str],
) -> list[QualityGateViolation]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise ProtectedScopeDiffError(
                f"Workspace row {workspace_id} disappeared; cannot load owned_paths "
                "for recovered dirty-worktree commit validation."
            )
        owned_paths = list(workspace.owned_paths)
    try:
        protected_file_diffs = await protected_file_diffs_for_committed_paths(
            self._deps.runner,
            worktree_path=worktree_path,
            base_ref=base_ref,
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read recovered dirty-worktree committed protected-scope "
            f"file contents for validation before treating the recovery as fixed: {exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=tuple(changed_paths),
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
    )


async def _resolve_task_tag(self: Any, workspace_id: str) -> str | None:
    """Load the workspace's optional Jira issue key for commit-message tagging."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return workspace.task_tag if workspace is not None else None


def _branch_name_to_ref(branch_name: str) -> str:
    return branch_name if branch_name.startswith("refs/") else f"refs/heads/{branch_name}"


async def _resolve_workspace_branch_ref(self: Any, workspace_id: str) -> str | None:
    """Load the workspace's expected local branch ref."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None or not workspace.branch_name:
            return None
        return _branch_name_to_ref(workspace.branch_name)


async def _recover_missing_head_object_from_filesystem(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    operation_start_head: str,
    task_tag: str | None = None,
    expected_branch_ref: str | None = None,
    command_evidence: Sequence[str] = (),
) -> str | None:
    """Rebuild a valid branch commit from filesystem state when HEAD's object is missing."""
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is None:
        return None

    async def mirror_git(args: list[str]) -> CommandResult:
        return cast(
            CommandResult,
            await self._deps.runner.run(
                ["git", "--git-dir", str(mirror_path), *args],
                env=git_env_without_object_lookup_overrides(),
            ),
        )

    async def worktree_git(args: list[str]) -> CommandResult:
        return cast(
            CommandResult,
            await self._deps.runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ],
                env=git_env_without_object_lookup_overrides(),
            ),
        )

    async def cleanup_after_abort(
        reason: str,
        *,
        untracked_cleanup_paths: Sequence[str] = (),
    ) -> None:
        cleanup = await worktree_git(["reset", "--hard", operation_start_head])
        if not cleanup.ok:
            _log.warning(
                "monitor.head_object_missing_recovery_abort_cleanup_failed",
                workspace_id=workspace_id,
                reason=reason,
                returncode=cleanup.returncode,
                stderr=cleanup.stderr[:400],
            )
        elif untracked_cleanup_paths:
            clean = await worktree_git(
                [
                    "--literal-pathspecs",
                    "clean",
                    "-fd",
                    "--",
                    *untracked_cleanup_paths,
                ]
            )
            if not clean.ok:
                _log.warning(
                    "monitor.head_object_missing_recovery_abort_clean_failed",
                    workspace_id=workspace_id,
                    reason=reason,
                    returncode=clean.returncode,
                    stderr=clean.stderr[:400],
                )

    start_ok = await mirror_git(["cat-file", "-e", f"{operation_start_head}^{{commit}}"])
    if not start_ok.ok:
        return None

    branch_ref = await _resolve_worktree_branch_ref(worktree_path)
    if branch_ref is None:
        return None
    expected_ref = expected_branch_ref or await _resolve_workspace_branch_ref(self, workspace_id)
    if expected_ref is None or branch_ref != expected_ref:
        _log.warning(
            "monitor.head_object_missing_recovery_branch_ref_mismatch",
            workspace_id=workspace_id,
            branch_ref=branch_ref,
            expected_branch_ref=expected_ref,
        )
        return None

    reset_ref = await mirror_git(["update-ref", branch_ref, operation_start_head])
    if not reset_ref.ok:
        return None

    if _worktree_has_merge_head(worktree_path):
        _log.warning(
            "monitor.head_object_missing_recovery_merge_in_progress",
            workspace_id=workspace_id,
            branch_ref=branch_ref,
        )
        return None

    reset_index = await worktree_git(["reset", "--mixed", "HEAD"])
    if not reset_index.ok:
        await cleanup_after_abort("reset_index_failed")
        return None

    add = await worktree_git(["add", "-A"])
    if not add.ok:
        await cleanup_after_abort("add_failed")
        return None

    staged = await worktree_git(["diff", "--cached", "--name-status", "-z"])
    if not staged.ok:
        await cleanup_after_abort("staged_diff_failed")
        return None
    staged_paths = list(_changed_paths_from_name_status_z(staged.stdout))
    staged_untracked_cleanup_paths = list(
        _untracked_cleanup_paths_from_name_status_z(staged.stdout)
    )
    excluded = [p for p in staged_paths if is_under_agent_runtime_root(p)]
    if excluded:
        unstage = await worktree_git(
            ["--literal-pathspecs", "reset", "-q", "HEAD", "--", *excluded]
        )
        if not unstage.ok:
            await cleanup_after_abort("runtime_path_unstage_failed")
            return None
        staged = await worktree_git(["diff", "--cached", "--name-status", "-z"])
        if not staged.ok:
            await cleanup_after_abort("staged_diff_after_runtime_path_unstage_failed")
            return None
        staged_paths = list(_changed_paths_from_name_status_z(staged.stdout))
        staged_untracked_cleanup_paths = list(
            _untracked_cleanup_paths_from_name_status_z(staged.stdout)
        )

    if staged_paths:
        policy_message = await self._refresh_supply_chain_policy_before_push(
            workspace_id=workspace_id,
            command_evidence=command_evidence,
            changed_paths=staged_paths,
        )
        if policy_message is not None:
            cleanup = await worktree_git(["reset", "--hard", operation_start_head])
            if not cleanup.ok:
                _log.warning(
                    "monitor.head_object_missing_recovery_policy_blocked_cleanup_failed",
                    workspace_id=workspace_id,
                    returncode=cleanup.returncode,
                    stderr=cleanup.stderr[:400],
                )
            elif staged_untracked_cleanup_paths:
                clean = await worktree_git(
                    [
                        "--literal-pathspecs",
                        "clean",
                        "-fd",
                        "--",
                        *staged_untracked_cleanup_paths,
                    ]
                )
                if not clean.ok:
                    _log.warning(
                        "monitor.head_object_missing_recovery_policy_blocked_clean_failed",
                        workspace_id=workspace_id,
                        returncode=clean.returncode,
                        stderr=clean.stderr[:400],
                    )
            raise _MonitorPolicyBlockedError(policy_message)
        commit = await self._deps.runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *git_identity_config_args(),
                "commit",
                "-m",
                commit_message_with_task_tag(
                    f"awf: recover {workspace_id} from missing git object", task_tag
                )[:72],
                "-m",
                (
                    f"AWF recovered workspace {workspace_id} after HEAD pointed at "
                    "a commit object missing from the canonical mirror. The commit "
                    "squashes the workspace filesystem state onto operation start "
                    f"head {operation_start_head[:10]}."
                ),
            ],
            env=git_env_without_object_lookup_overrides(),
        )
        if not commit.ok:
            await cleanup_after_abort(
                "commit_failed",
                untracked_cleanup_paths=staged_untracked_cleanup_paths,
            )
            return None

    await asyncio.to_thread(repair_agent_writable_worktree, mirror_path, worktree_path)

    head = await worktree_git(["rev-parse", "HEAD"])
    recovered_head_sha = head.stdout.strip()
    if not head.ok or not recovered_head_sha:
        await cleanup_after_abort("recovered_head_unavailable")
        return None
    recovered_head_ok = await mirror_git(["cat-file", "-e", f"{recovered_head_sha}^{{commit}}"])
    if not recovered_head_ok.ok:
        await cleanup_after_abort("recovered_head_missing_from_mirror")
        return None
    return recovered_head_sha


async def _cleanup_recovered_missing_head_delta(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    recovery_head: str,
    reason: str,
    untracked_cleanup_paths: Sequence[str] = (),
) -> None:
    cleanup = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", recovery_head),
        env=git_env_without_object_lookup_overrides(),
    )
    if not cleanup.ok:
        _log.warning(
            "monitor.head_object_missing_recovered_cleanup_failed",
            workspace_id=workspace_id,
            reason=reason,
            returncode=cleanup.returncode,
            stderr=cleanup.stderr[:400],
        )
        return
    if not untracked_cleanup_paths:
        return
    clean = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "--literal-pathspecs",
            "clean",
            "-fd",
            "--",
            *untracked_cleanup_paths,
        ),
        env=git_env_without_object_lookup_overrides(),
    )
    if not clean.ok:
        _log.warning(
            "monitor.head_object_missing_recovered_clean_failed",
            workspace_id=workspace_id,
            reason=reason,
            returncode=clean.returncode,
            stderr=clean.stderr[:400],
        )


def _untracked_cleanup_paths_from_name_status_z(diff_stdout: str) -> tuple[str, ...]:
    if not diff_stdout:
        return ()
    fields = diff_stdout.split("\0")
    if fields[-1] != "":
        raise ProtectedScopeDiffError(
            "truncated `--name-status -z` output: missing terminating NUL"
        )
    fields = fields[:-1]

    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            raise ProtectedScopeDiffError("malformed `--name-status -z` output: empty status field")
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise ProtectedScopeDiffError(
                f"truncated `--name-status -z` record for status {status!r}"
            )
        record_paths = fields[index : index + path_count]
        index += path_count
        if any(not path for path in record_paths):
            raise ProtectedScopeDiffError(
                f"malformed `--name-status -z` record for status {status!r}"
            )
        if status.startswith("A"):
            paths.append(record_paths[0])
        elif status.startswith(("R", "C")):
            paths.append(record_paths[1])
    return tuple(dict.fromkeys(paths))


def _worktree_git_dir(worktree_path: Path) -> Path | None:
    git_path = worktree_path / ".git"
    if git_path.is_dir():
        return git_path
    try:
        git_file = git_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = worktree_path / git_dir
    return git_dir


def _worktree_has_merge_head(worktree_path: Path) -> bool:
    git_dir = _worktree_git_dir(worktree_path)
    return git_dir is not None and (git_dir / "MERGE_HEAD").exists()


async def _resolve_worktree_branch_ref(worktree_path: Path) -> str | None:
    """Resolve the full branch ref for a worktree."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        "symbolic-ref",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_env_without_object_lookup_overrides(),
    )
    stdout_bytes, _ = await proc.communicate()
    assert proc.returncode is not None
    if proc.returncode != 0:
        return None
    return stdout_bytes.decode("utf-8", errors="replace").strip()


async def _resolve_block_resume_phase(self: Any, workspace_id: str) -> str | None:
    """Load the workspace's recorded protected-scope block resume phase.

    Persisted by ``enter_blocked_for_protected_violation_in_session`` and used to
    discriminate a sync-base-originated pause (``monitor_protected_scope_sync_base``)
    from a generic push pause or a no-block remonitor when selecting the
    protected-scope validator on an operator-hint resume."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return workspace.block_resume_phase if workspace is not None else None


async def _clear_block_resume_phase(self: Any, workspace_id: str) -> None:
    """Clear the recorded protected-scope block resume phase once its resume settles.

    The phase column discriminates a sync-base-originated pause
    (``monitor_protected_scope_sync_base``) when ``_run_operator_hint_cycle`` selects
    the protected-scope validator. It is set at block time and never overwritten
    except by a fresh block. A later operator-hint or remonitor cycle on
    ``monitoring_pr`` arms a hint WITHOUT re-blocking, so the stale sync-base phase
    would still select the sync-base-aware validator — letting a repair that reverts
    an unowned protected file back to base contents push without a grant or re-block.
    Reset it to ``None`` once the resume is finalized so the next cycle falls back to
    the generic unpushed-commit validator (PRRT_kwDOSJAM6s6KFqEg)."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None or workspace.block_resume_phase is None:
            return
        workspace.block_resume_phase = None
        await session.commit()


async def _commit_dirty_worktree(
    self: Any,
    *,
    workspace_id: str,
    message: str,
    compose_project: str | None = None,
    compose_file: Path | None = None,
    state: MonitorState | None = None,
    command_evidence: Sequence[str] = (),
    protected_scope_revert_remote_branch: str | None = None,
    remote_push_url: str | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> bool:
    """Commit dirty monitor-agent edits so PR feedback is not stranded.

    Coding CLIs can apply a valid fix and still exit non-zero while
    formatting, testing, or summarising. PR #35 exposed that failure
    mode: the monitor treated the CLI failure as a bot defer, but the
    useful fix was left dirty in the service worktree and never pushed.
    """

    worktree_path = self._worktrees_root / workspace_id
    if not worktree_path.exists():
        return False

    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            log_kwargs: dict[str, object] = {
                "workspace_id": workspace_id,
                "reason_code": _MIRROR_HOOKS_PATH_POISONED_REASON,
                "error_type": exc.__class__.__name__,
            }
            if isinstance(exc, GitOperationError):
                log_kwargs.update(
                    {
                        "repair_reason_code": exc.reason_code,
                        "git_operation": exc.operation,
                        "git_returncode": exc.returncode,
                        "stderr": exc.stderr[:1000],
                    }
                )
            else:
                log_kwargs["error"] = str(exc)
            _log.warning(
                "monitor.mirror_hooks_path_repair_failed",
                **log_kwargs,
            )
            raise _MonitorMirrorHooksPathRepairFailedError() from exc

    head_object_exists = await verify_head_object_exists(worktree_path)
    if not head_object_exists:
        _log.warning(
            "monitor.head_object_missing",
            workspace_id=workspace_id,
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        recovery_head = operation_start_head
        candidate_head: str | None = None
        attempted_mirror_recovery_heads: set[str] = set()
        attempted_worktree_recovery_heads: set[str] = set()

        async def verified_mirror_recovery_head(
            recovery_head_sha: str,
            *,
            source: str,
        ) -> str | None:
            attempted_mirror_recovery_heads.add(recovery_head_sha)
            recovery_head_exists = await _mirror_commit_object_exists(
                self, cast(Path, mirror_path), recovery_head_sha
            )
            if recovery_head_exists:
                return recovery_head_sha
            _log.warning(
                "monitor.head_object_missing_recovery_anchor_missing",
                workspace_id=workspace_id,
                operation_start_head=recovery_head_sha[:10],
                recovery_source=source,
            )
            return None

        async def verified_worktree_recovery_head(
            recovery_head_sha: str,
            *,
            source: str,
        ) -> str | None:
            attempted_worktree_recovery_heads.add(recovery_head_sha)
            recovery_head_exists = await _worktree_commit_object_exists(
                self, worktree_path, recovery_head_sha
            )
            if recovery_head_exists:
                return recovery_head_sha
            _log.warning(
                "monitor.head_object_missing_recovery_anchor_missing",
                workspace_id=workspace_id,
                operation_start_head=recovery_head_sha[:10],
                recovery_source=source,
            )
            return None

        if recovery_head and mirror_path is not None:
            recovery_head = await verified_mirror_recovery_head(
                recovery_head,
                source="operation_start_head",
            )
        elif recovery_head:
            recovery_head = await verified_worktree_recovery_head(
                recovery_head,
                source="operation_start_head",
            )
        if not recovery_head:
            candidate_head = candidate_head or await _open_merge_candidate_head_sha(
                self, workspace_id
            )
            recovery_head = candidate_head
            if recovery_head and mirror_path is None:
                if recovery_head in attempted_worktree_recovery_heads:
                    recovery_head = None
                else:
                    recovery_head = await verified_worktree_recovery_head(
                        recovery_head,
                        source="candidate",
                    )
            elif recovery_head and mirror_path is not None:
                if recovery_head in attempted_mirror_recovery_heads:
                    recovery_head = None
                else:
                    recovery_head = await verified_mirror_recovery_head(
                        recovery_head,
                        source="candidate",
                    )
        if recovery_head is None:
            raise _MonitorHeadObjectMissingError(
                _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                f"HEAD object missing for workspace {workspace_id} and no recovery head available",
            )
        recovered = await _recover_missing_head_object_from_filesystem(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            operation_start_head=recovery_head,
            command_evidence=command_evidence,
            task_tag=(
                await _resolve_task_tag(self, workspace_id)
                if isinstance(task_tag, _TaskTagUnset)
                else task_tag
            ),
        )
        if recovered is None:
            raise _MonitorHeadObjectMissingError(
                _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                f"HEAD object missing for workspace {workspace_id} and recovery failed",
            )
        _log.info(
            "monitor.head_object_missing_recovered",
            workspace_id=workspace_id,
            recovered_head=recovered[:10],
            reason_code=_HEAD_OBJECT_MISSING_RECOVERED_REASON,
        )
        if recovered != recovery_head:
            diff = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "diff",
                    "--name-status",
                    "-z",
                    f"{recovery_head}..{recovered}",
                    "--",
                ),
                env=git_env_without_object_lookup_overrides(),
            )
            if not diff.ok:
                _log.warning(
                    "monitor.head_object_missing_recovered_diff_failed",
                    workspace_id=workspace_id,
                    returncode=diff.returncode,
                    stderr=diff.stderr[:400],
                    reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                )
                raise _MonitorHeadObjectMissingError(
                    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                    f"HEAD object recovered for workspace {workspace_id} but recovered diff failed",
                )
            try:
                recovered_untracked_cleanup_paths = _untracked_cleanup_paths_from_name_status_z(
                    diff.stdout
                )
                recovered_diff_paths = _changed_paths_from_name_status_z(diff.stdout)
            except ProtectedScopeDiffError as exc:
                _log.warning(
                    "monitor.head_object_missing_recovered_diff_failed",
                    workspace_id=workspace_id,
                    returncode=diff.returncode,
                    stderr=str(exc)[:400],
                    reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                )
                raise _MonitorHeadObjectMissingError(
                    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                    f"HEAD object recovered for workspace {workspace_id} but recovered diff was malformed",
                ) from exc
            recovered_paths = [
                p for p in recovered_diff_paths if not is_under_agent_runtime_root(p)
            ]
            if not recovered_paths:
                return False
            if not await repair_agent_runtime_ownership(
                logger=_log,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                reason="dirty_worktree_pre_commit",
                event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
                reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            ):
                raise _MonitorAgentRuntimeOwnershipRepairFailedError(
                    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
                )
            recovered_violations = await _protected_scope_violations_for_recovered_dirty_commit(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                base_ref=recovery_head,
                changed_paths=tuple(recovered_paths),
            )
            if recovered_violations:
                _log.warning(
                    "monitor.head_object_missing_recovered_protected_scope_blocked",
                    workspace_id=workspace_id,
                    recovered_head=recovered[:10],
                    paths=[violation.path for violation in recovered_violations],
                )
                await _cleanup_recovered_missing_head_delta(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    recovery_head=recovery_head,
                    reason="protected_scope_blocked",
                    untracked_cleanup_paths=recovered_untracked_cleanup_paths,
                )
                raise _MonitorPolicyBlockedError(
                    quality_gate_violation_message(recovered_violations),
                    reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
                )
            if compose_project is not None and compose_file is not None:
                recovered_status_stdout = "".join(f" M {path}\n" for path in recovered_paths)
                repaired_status = await self._repair_protected_scope_changes_before_commit(
                    workspace_id=workspace_id,
                    status_stdout=recovered_status_stdout,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    protected_scope_revert_remote_branch=(protected_scope_revert_remote_branch),
                    remote_push_url=remote_push_url,
                )
                if repaired_status is None:
                    return False
                post_repair_status = await self._deps.runner.run(
                    git_worktree_command(
                        worktree_path,
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ),
                    env=git_env_without_object_lookup_overrides(),
                )
                if not post_repair_status.ok:
                    _log.warning(
                        "monitor.dirty_stage_status_failed",
                        workspace_id=workspace_id,
                        stderr=post_repair_status.stderr[:400],
                    )
                    return False
                post_repair_untracked = set(
                    _untracked_paths_from_porcelain(post_repair_status.stdout)
                )
                post_repair_paths = tuple(
                    path
                    for path in _changed_paths_from_porcelain(post_repair_status.stdout)
                    if not (path in post_repair_untracked and is_under_agent_runtime_root(path))
                )
                if post_repair_paths:
                    repair_residue_committed = await self._commit_dirty_worktree(
                        workspace_id=workspace_id,
                        message=message,
                        command_evidence=command_evidence,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        state=state,
                        protected_scope_revert_remote_branch=(protected_scope_revert_remote_branch),
                        remote_push_url=remote_push_url,
                        operation_start_head=recovered,
                        task_tag=task_tag,
                    )
                    return bool(repair_residue_committed)
            return True
    # Decide dirtiness with the SAME untracked AWF-agent-runtime exclusion the
    # pre-existing-dirty guard and the staging filter below apply. A worktree
    # dirtied only by reviewer subagent memory (untracked ``.claude/agent-memory/...``)
    # must short-circuit here, BEFORE any commit-side effects — supply-chain policy
    # refresh, agent-runtime ownership repair, and protected-scope repair (which can
    # launch the agent CLI) — exactly as the guard and staging logic intentionally
    # skip it. ``--untracked-files=all`` is load-bearing: with git's default
    # ``normal`` mode a fully-untracked ``.claude/`` collapses to a single
    # ``?? .claude/`` entry that is NOT under ``.claude/agent-memory/`` and so would
    # escape the agent-runtime filter, letting memory-only dirt fall through into the
    # side-effecting path. Enumerating leaf paths lets the filter drop the memory files.
    status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all"),
        env=git_env_without_object_lookup_overrides(),
    )
    if not status.ok:
        _log.warning(
            "monitor.dirty_check_failed",
            workspace_id=workspace_id,
            stderr=status.stderr[:400],
        )
        return False
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    changed_paths = tuple(
        path
        for path in _changed_paths_from_porcelain(status.stdout)
        if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not changed_paths:
        return False

    policy_message = await self._refresh_supply_chain_policy_before_push(
        workspace_id=workspace_id,
        command_evidence=command_evidence,
        changed_paths=changed_paths,
    )
    if policy_message is not None:
        raise _MonitorPolicyBlockedError(policy_message)

    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="dirty_worktree_pre_commit",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    if compose_project is not None and compose_file is not None:
        repaired_status = await self._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=status.stdout,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            protected_scope_revert_remote_branch=protected_scope_revert_remote_branch,
            remote_push_url=remote_push_url,
        )
        if repaired_status is None:
            return False

    # The pre-existing-dirty guard (``_pre_existing_dirty_repair_worktree_result``)
    # lets a repair run when the only dirt is UNTRACKED AWF-agent-runtime memory
    # (reviewer subagent ``.claude/agent-memory/...`` files), which never belongs
    # to the PR. The commit path must apply the SAME exclusion before staging, or a
    # blind ``git add -A`` would stage that pre-existing memory back into the PR.
    # ``--untracked-files=all`` is load-bearing here, exactly as in the guard: with
    # git's default ``normal`` mode a fully-untracked ``.claude/`` collapses to a
    # single ``?? .claude/`` entry that escapes the agent-runtime filter; enumerating
    # leaf paths lets the filter drop the memory files. If nothing else remains to
    # stage, there is no PR-worthy change — return False like the clean path above.
    stage_status = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "--untracked-files=all"),
        env=git_env_without_object_lookup_overrides(),
    )
    if not stage_status.ok:
        _log.warning(
            "monitor.dirty_stage_status_failed",
            workspace_id=workspace_id,
            stderr=stage_status.stderr[:400],
        )
        return False
    stage_untracked = set(_untracked_paths_from_porcelain(stage_status.stdout))
    stage_paths = sorted(
        path
        for path in _changed_paths_from_porcelain(stage_status.stdout)
        if not (path in stage_untracked and is_under_agent_runtime_root(path))
    )
    if not stage_paths:
        return False

    add = await self._deps.runner.run(
        git_worktree_command(worktree_path, "--literal-pathspecs", "add", "-A", "--", *stage_paths),
        env=git_env_without_object_lookup_overrides(),
    )
    if not add.ok:
        _log.warning(
            "monitor.dirty_add_failed",
            workspace_id=workspace_id,
            stderr=add.stderr[:400],
        )
        return False

    cached = await self._deps.runner.run(
        git_worktree_command(worktree_path, "diff", "--cached", "--quiet"),
        env=git_env_without_object_lookup_overrides(),
    )
    if cached.returncode == 0:
        return False

    # Prepend the workspace's Jira issue key (if any) so monitor review-fix /
    # CI-fix commits link to the issue. Idempotent: a re-run never double-prefixes.
    # Truncate to [:72] after tagging for parity with every other AWF-authored
    # commit subject (executor agent/recovery commits, post-validation conformance).
    # The caller (a repair path) resolves ``task_tag`` once per monitor cycle and
    # threads it in; fall back to a self-resolve only when nothing was threaded
    # (the sentinel default), preserving behavior for callers that do not pass it.
    resolved_task_tag = (
        await _resolve_task_tag(self, workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    message = commit_message_with_task_tag(message, resolved_task_tag)[:72]

    commit = await self._deps.runner.run(
        git_worktree_command(worktree_path, "commit", "-m", message),
        env=git_env_without_object_lookup_overrides(),
    )
    if not commit.ok:
        if not await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            reason="dirty_worktree_post_commit_failed",
            event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
        ):
            _log.warning(
                "monitor.dirty_worktree_post_commit_ownership_repair_failed",
                workspace_id=workspace_id,
                commit_stderr=commit.stderr[:400],
            )
            raise _MonitorAgentRuntimeOwnershipRepairFailedError(
                AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
            )
        # Scope the autofix retry to the paths we actually staged. ``stage_paths``
        # is the leaf-enumerated (``--untracked-files=all``), agent-runtime-filtered
        # set computed above, so it never carries untracked ``.claude/agent-memory/``
        # leftovers or a collapsed ``?? .claude/`` directory entry into
        # ``operation_dirty_paths`` — which would otherwise widen the retry's
        # in-scope check beyond what this operation committed.
        retry = await _retry_monitor_precommit_autofix_commit_once(
            runner=self._deps.runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            message=message,
            commit_result=commit,
            operation_dirty_paths=stage_paths,
        )
        if retry is None:
            _log.warning(
                "monitor.dirty_commit_failed",
                workspace_id=workspace_id,
                stderr=commit.stderr[:400],
            )
            return False

        retry_commit, restaged_paths = retry
        if not retry_commit.ok:
            _log.warning(
                "monitor.dirty_commit_autofix_retry_failed",
                workspace_id=workspace_id,
                restaged_paths=list(restaged_paths),
                stderr=retry_commit.stderr[:400],
            )
            _log.warning(
                "monitor.dirty_commit_failed",
                workspace_id=workspace_id,
                stderr=commit.stderr[:400],
            )
            return False
        _log.info(
            "monitor.dirty_commit_autofix_retry_succeeded",
            workspace_id=workspace_id,
            restaged_paths=list(restaged_paths),
        )
    _log.info("monitor.dirty_worktree_committed", workspace_id=workspace_id)

    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="dirty_worktree_post_commit_succeeded",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
        reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    ):
        _log.warning(
            "monitor.dirty_worktree_post_commit_succeeded_ownership_repair_failed",
            workspace_id=workspace_id,
        )
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )
    return True
