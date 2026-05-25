"""Git operations and salvage handling for WorkspaceExecutor."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awf.control.executor import WorkspaceExecutor
    from awf.control.executor.shared import _ConformanceSalvageExecutionResult
    from awf.db.models import Workspace

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.git_identity import git_identity_config_args, git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.node.git_manager import mirror_path_for_worktree, repair_agent_writable_worktree
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE,
    CONFORMANCE_SALVAGE_APPLIED_REASON,
    CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE,
    CONFORMANCE_SALVAGE_CONFLICT_REASON,
    SALVAGE_PATCH_APPLY_FAILED,
    SALVAGE_PATCH_DIGEST_MISMATCH,
    SALVAGE_PATCH_UNAVAILABLE,
    build_conformance_salvage_conflict_prompt,
    conformance_salvage_from_task_policy,
)

_log = get_logger(__name__)

GIT_AGENT_WRITABILITY_FAILED_REASON_CODE = "GIT_AGENT_WRITABILITY_FAILED"
_REBASE_RECOVERY_OPERATION_IDENTITY_KEYS = (
    "source",
    "recovery_mode",
    "reason_code",
    "pr_number",
    "source_head_sha",
    "source_base_sha",
)


@dataclass(frozen=True)
class _GitObjectRecoveryResult:
    broken_head_sha: str | None
    recovered_head_sha: str
    strategy: str = "filesystem_tree_commit"


def _git_error_indicates_missing_head_object(text: str) -> bool:
    lower = text.lower()
    return (
        "bad object head" in lower
        or "not a valid object name head" in lower
        or "could not parse object 'head'" in lower
    )


def _agent_git_writability_preflight_script(workspace_id: str) -> str:
    quoted_workspace_id = shlex.quote(workspace_id)
    return f"""
set -eu
workspace_id={quoted_workspace_id}
git status --porcelain >/dev/null
blob=$(printf 'awf git preflight %s\\n' "$workspace_id" | git hash-object -w --stdin)
git cat-file -e "$blob^{{blob}}"
ref="refs/awf/preflight/$workspace_id"
git update-ref "$ref" HEAD
git update-ref -d "$ref"
""".strip()


async def _recover_missing_head_from_filesystem(
    *,
    runner: AsyncCommandRunner,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    branch_name: str,
) -> _GitObjectRecoveryResult | None:
    """Rebuild a valid AWF branch commit from files when HEAD points to a missing object."""
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is None:
        return None
    branch_ref = branch_name if branch_name.startswith("refs/") else f"refs/heads/{branch_name}"
    broken_head_sha = _read_ref_sha(mirror_path, branch_ref)

    async def mirror_git(args: list[str]) -> CommandResult:
        return await runner.run(["git", "--git-dir", str(mirror_path), *args])

    async def worktree_git(args: list[str]) -> CommandResult:
        return await runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *args,
            ]
        )

    base_ok = await mirror_git(["cat-file", "-e", f"{base_commit}^{{commit}}"])
    if not base_ok.ok:
        return None
    reset_ref = await mirror_git(["update-ref", branch_ref, base_commit])
    if not reset_ref.ok:
        return None
    reset_index = await worktree_git(["reset", "--mixed", "HEAD"])
    if not reset_index.ok:
        return None
    add = await worktree_git(["add", "-A"])
    if not add.ok:
        return None
    diff = await worktree_git(["diff", "--cached", "--quiet"])
    if diff.returncode not in {0, 1}:
        return None
    if diff.returncode == 0:
        return None
    commit = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            *git_identity_config_args(),
            "commit",
            "-m",
            f"awf: recover {workspace_id} from missing git object"[:72],
            "-m",
            (
                f"AWF recovered workspace {workspace_id} after HEAD pointed at "
                "a commit object missing from the canonical mirror. The commit "
                f"squashes the workspace filesystem state onto base {base_commit[:10]}."
            ),
        ]
    )
    if not commit.ok:
        return None
    await asyncio.to_thread(repair_agent_writable_worktree, mirror_path, worktree_path)
    head = await worktree_git(["rev-parse", "HEAD"])
    recovered_head_sha = head.stdout.strip()
    if not head.ok or not recovered_head_sha:
        return None
    return _GitObjectRecoveryResult(
        broken_head_sha=broken_head_sha,
        recovered_head_sha=recovered_head_sha,
    )


def _read_ref_sha(mirror_path: Path, ref: str) -> str | None:
    ref_path = mirror_path / ref
    try:
        value = ref_path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    packed_refs = mirror_path / "packed-refs"
    try:
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        return None
    return None


def _rebase_recovery_operation_payload_identities(
    recovery_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    from awf.control.executor.shared import _VALIDATE_ONLY_RECOVERY_SOURCES

    recovery_payload_dict = dict(recovery_payload)
    identity: dict[str, Any] = {
        key: recovery_payload_dict[key]
        for key in _REBASE_RECOVERY_OPERATION_IDENTITY_KEYS
        if key in recovery_payload_dict
    }
    identity.setdefault("recovery_mode", "rebase_only")
    if "source" in identity:
        return (identity,)
    return tuple(
        {**identity, "source": source} for source in sorted(_VALIDATE_ONLY_RECOVERY_SOURCES)
    )


def _git_name_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


async def _repair_agent_git_ownership(
    executor: WorkspaceExecutor,
    *,
    workspace_id: str,
    worktree_path: Path,
    reason: str,
) -> bool:
    _ = executor
    try:
        await asyncio.to_thread(repair_agent_writable_worktree, None, worktree_path)
    except Exception:
        _log.exception(
            "executor.agent_git_ownership_repair_failed",
            workspace_id=workspace_id,
            worktree_path=str(worktree_path),
            reason=reason,
        )
        return False
    return True


async def _prepare_conformance_salvage_for_execution(
    executor: WorkspaceExecutor,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
) -> _ConformanceSalvageExecutionResult | None:
    from awf.control.executor.shared import _ConformanceSalvageExecutionResult

    salvage = conformance_salvage_from_task_policy(workspace.task_policy)
    if salvage is None:
        return None

    patch_path_value = salvage.get("patch_path")
    expected_sha = salvage.get("patch_sha256")
    if not isinstance(patch_path_value, str) or not patch_path_value.strip():
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message="conformance salvage patch path is missing",
            salvage=salvage,
        )
    if not isinstance(expected_sha, str) or not expected_sha.strip():
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_DIGEST_MISMATCH,
            message="conformance salvage patch digest is missing",
            salvage=salvage,
        )

    patch_path = Path(patch_path_value)
    if not patch_path.is_file():
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message=f"conformance salvage patch is unavailable: {patch_path}",
            salvage=salvage,
        )

    patch_bytes = patch_path.read_bytes()
    actual_sha = hashlib.sha256(patch_bytes).hexdigest()
    if actual_sha != expected_sha:
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_DIGEST_MISMATCH,
            message=(
                "conformance salvage patch digest mismatch "
                f"(expected={expected_sha}, actual={actual_sha})"
            ),
            salvage=salvage,
        )

    async def git(args: list[str]) -> CommandResult:
        return await executor._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                *args,
            ]
        )

    check = await git(["apply", "--check", str(patch_path)])
    if check.ok:
        applied = await git(["apply", str(patch_path)])
        if not applied.ok:
            return await executor._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_APPLY_FAILED,
                message=(
                    "conformance salvage patch passed preflight but failed to apply: "
                    f"{applied.stderr or applied.stdout}"
                )[:2000],
                salvage=salvage,
            )
        await executor._record_conformance_salvage_event(
            workspace_id=workspace_id,
            event_type=CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE,
            reason_code=CONFORMANCE_SALVAGE_APPLIED_REASON,
            payload={
                "conformance_salvage": salvage,
                "patch_sha256": actual_sha,
                "implementation_paths": salvage.get("implementation_paths", []),
            },
        )
        return _ConformanceSalvageExecutionResult(status="applied")

    agent_patch_path = executor._materialize_salvage_patch_for_agent(
        worktree_path=worktree_path,
        patch_path=patch_path,
        patch_bytes=patch_bytes,
    )
    apply_error = (check.stderr or check.stdout or "git apply --check failed").strip()
    await executor._record_conformance_salvage_event(
        workspace_id=workspace_id,
        event_type=CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE,
        reason_code=CONFORMANCE_SALVAGE_CONFLICT_REASON,
        payload={
            "conformance_salvage": salvage,
            "agent_patch_path": agent_patch_path,
            "apply_error": apply_error[:2000],
        },
    )
    return _ConformanceSalvageExecutionResult(
        status="conflict",
        prompt_override=build_conformance_salvage_conflict_prompt(
            task_prompt=workspace.task_prompt,
            salvage=salvage,
            agent_patch_path=agent_patch_path,
            apply_error=apply_error,
        ),
    )
