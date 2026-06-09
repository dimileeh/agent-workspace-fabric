"""Git operations and salvage handling for WorkspaceExecutor."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awf.control.executor import WorkspaceExecutor
    from awf.db.models import Workspace

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.git_identity import git_identity_config_args, git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.control.executor.constants import _VALIDATE_ONLY_RECOVERY_SOURCES
from awf.control.executor.types import _ConformanceSalvageExecutionResult
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
    if diff.returncode == 1:
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


async def _recover_branch_drift(
    *,
    git_in_worktree: Callable[[list[str]], Awaitable[CommandResult]],
    workspace_id: str,
    expected_branch: str,
) -> None:
    """Fast-forward the expected AWF branch onto an agent-drifted branch tip.

    Mechanically extracted from ``execution_flow.execute``'s post-agent commit
    step; behavior is unchanged. Raises ``RuntimeError`` on any unrecoverable
    git failure so the caller's post-agent commit ``try`` block routes it
    through the standard infrastructure-failure path.
    """
    # ── Branch-drift recovery ──────────────────────────────────
    # Agent CLIs (Claude Code, Codex) sometimes run
    # ``git checkout -b <descriptive-name>`` mid-session as part
    # of "good git hygiene" — they don't know AWF already
    # created the right branch for them. If they commit on the
    # drifted branch, ``pr_creator.push_and_open`` pushes the
    # original (empty) AWF branch to origin and ``gh pr create``
    # fails with "No commits between development and awf/ws_...".
    #
    # Incident 2026-04-24 (T41 Phase 3, ws_9ca6134a): agent
    # switched to ``awf/t41-phase3-github-app-install-flow``
    # and committed there. AWF's push of the empty
    # ``awf/ws_9ca6134a...`` created a no-op PR. Agent's work
    # stranded in the worktree.
    #
    # Recovery: if HEAD's branch diverged, fast-forward the
    # expected branch to the agent's tip. Both branches share
    # the same base commit (the worktree was created fresh
    # from origin/<base>), so this is a safe pointer update.
    current_branch_r = await git_in_worktree(["rev-parse", "--abbrev-ref", "HEAD"])
    # If rev-parse itself failed (corrupted git state, missing
    # HEAD, etc.) we can't reliably detect drift. Fail loudly
    # rather than silently skip — a working tree that can't
    # resolve HEAD is broken enough that continuing to push
    # would produce nonsense anyway.
    if not current_branch_r.ok:
        raise RuntimeError(
            "branch drift check: ``git rev-parse --abbrev-ref HEAD`` "
            f"failed with exit {current_branch_r.returncode}: "
            f"{current_branch_r.stderr!r}"
        )
    current_branch = (current_branch_r.stdout or "").strip()
    if current_branch and current_branch != expected_branch:
        _log.warning(
            "executor.branch_drift_detected",
            workspace_id=workspace_id,
            current_branch=current_branch,
            expected_branch=expected_branch,
        )
        agent_head_r = await git_in_worktree(["rev-parse", "HEAD"])
        agent_head = (agent_head_r.stdout or "").strip()
        if not agent_head_r.ok or not agent_head:
            raise RuntimeError(
                f"branch drift detected (current={current_branch} "
                f"expected={expected_branch}) but agent HEAD could not "
                f"be resolved: {agent_head_r.stderr!r}"
            )
        # Preserve uncommitted changes. The executor's core
        # contract is "salvage whatever the agent left on
        # disk" — including edits not yet committed on the
        # drifted branch. ``status --porcelain`` with
        # ``--untracked-files=all`` catches both staged,
        # unstaged, and untracked files. If any exist, stash
        # them (with ``-u`` to include untracked) before the
        # ``switch`` so they don't get lost. Pop after the
        # fast-forward so they end up on top of the agent's
        # commits on the expected branch.
        status_r = await git_in_worktree(["status", "--porcelain=v1", "--untracked-files=all"])
        if not status_r.ok:
            raise RuntimeError(f"branch drift recovery: ``git status`` failed: {status_r.stderr!r}")
        has_wip = bool(status_r.stdout.strip())
        stash_created = False
        if has_wip:
            stash_r = await git_in_worktree(
                [
                    "stash",
                    "push",
                    "--include-untracked",
                    "--message",
                    f"awf-drift-recovery-{workspace_id}",
                ]
            )
            if not stash_r.ok:
                raise RuntimeError(
                    f"branch drift recovery: ``git stash push`` failed: "
                    f"{stash_r.stderr!r} (refusing to switch with dirty "
                    f"worktree that couldn't be stashed)"
                )
            stash_created = True

        # Switch to the expected branch. It should exist
        # locally — AWF created it at worktree-add time.
        switch_r = await git_in_worktree(["switch", expected_branch])
        if not switch_r.ok:
            # Best-effort: try to restore stashed WIP so it's
            # not silently lost before bailing out.
            if stash_created:
                await git_in_worktree(["stash", "pop"])
            raise RuntimeError(
                f"branch drift recovery: could not switch back to "
                f"{expected_branch}: {switch_r.stderr!r}"
            )
        # Fast-forward the expected branch to the agent's tip
        # using ``merge --ff-only``. The two branches share
        # the same base (AWF created the worktree fresh from
        # ``origin/<base>``) and the agent only added commits
        # on top, so ff must succeed. ``merge --ff-only`` over
        # ``reset --hard`` because the latter would also wipe
        # any WIP the user has in the working tree if the
        # stash step above silently did nothing (e.g. if
        # ``status`` missed an edge case).
        merge_r = await git_in_worktree(["merge", "--ff-only", agent_head])
        if not merge_r.ok:
            if stash_created:
                await git_in_worktree(["stash", "pop"])
            raise RuntimeError(
                f"branch drift recovery: ``merge --ff-only "
                f"{agent_head[:10]}`` failed: {merge_r.stderr!r}"
            )

        if stash_created:
            pop_r = await git_in_worktree(["stash", "pop"])
            if not pop_r.ok:
                # A pop conflict means the agent's WIP and the
                # fast-forwarded commits touch the same
                # regions. That's a real problem for the
                # workspace — the WIP is left in the stash
                # under a named entry, but we can't auto-merge
                # it. Fail loudly so the operator knows to
                # inspect.
                raise RuntimeError(
                    f"branch drift recovery: ``git stash pop`` failed "
                    f"(WIP conflicts with recovered commits): "
                    f"{pop_r.stderr!r}"
                )

        _log.info(
            "executor.branch_drift_recovered",
            workspace_id=workspace_id,
            recovered_from=current_branch,
            recovered_to=expected_branch,
            head_sha=agent_head,
            wip_stashed=has_wip,
        )


async def _prepare_conformance_salvage_for_execution(
    executor: WorkspaceExecutor,
    *,
    workspace_id: str,
    workspace: Workspace,
    worktree_path: Path,
) -> _ConformanceSalvageExecutionResult | None:
    salvage = conformance_salvage_from_task_policy(workspace.task_policy)
    if salvage is None:
        return None

    patch_path_value = salvage.get("patch_path")
    expected_sha = salvage.get("patch_sha256")
    salvage_artifacts_dir = executor._config.compose_projects_root.parent / "artifacts" / "salvage"
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

    try:
        patch_path = Path(patch_path_value).resolve()
        if not patch_path.is_relative_to(salvage_artifacts_dir):
            return await executor._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_UNAVAILABLE,
                message=(
                    "conformance salvage patch path is outside managed artifact directory: "
                    f"{patch_path}"
                ),
                salvage=salvage,
            )
    except OSError as exc:
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message=f"conformance salvage patch path is unavailable: {exc}",
            salvage=salvage,
        )

    if not patch_path.is_file():
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message=f"conformance salvage patch is unavailable: {patch_path}",
            salvage=salvage,
        )

    try:
        patch_bytes = patch_path.read_bytes()
    except OSError as exc:
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message=f"conformance salvage patch is unavailable: {exc}",
            salvage=salvage,
        )
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

    try:
        check = await git(["apply", "--check", str(patch_path)])
    except OSError as exc:
        return await executor._fail_conformance_salvage_execution(
            workspace_id=workspace_id,
            reason_code=SALVAGE_PATCH_UNAVAILABLE,
            message=f"conformance salvage patch is unavailable: {exc}",
            salvage=salvage,
        )
    if check.ok:
        try:
            applied = await git(["apply", str(patch_path)])
        except OSError as exc:
            return await executor._fail_conformance_salvage_execution(
                workspace_id=workspace_id,
                reason_code=SALVAGE_PATCH_UNAVAILABLE,
                message=f"conformance salvage patch is unavailable: {exc}",
                salvage=salvage,
            )
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
