"""Recovery for interrupted, unpublished PR comment repairs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.node.git_manager import (
    GitOperationError,
    git_env_without_object_lookup_overrides,
    linked_worktree_git_dir,
    linked_worktree_path_from_git_dir,
    mirror_path_for_worktree,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z
from awf.runtime.pr_monitor_runner.remote_ops import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

_COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED = "COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED"
_COMMENT_REPAIR_ROLLBACK_FAILED = "COMMENT_REPAIR_ROLLBACK_FAILED"
_COMMENT_REPAIR_UNPUBLISHED_ABANDONED = "COMMENT_REPAIR_UNPUBLISHED_ABANDONED"


def _verified_awf_comment_repair_worktree(
    *,
    runner: Any,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """Verify the exact AWF worktree and its reciprocal Git metadata link."""
    try:
        expected = (runner._worktrees_root / workspace_id).resolve()
        actual = worktree_path.resolve()
    except (OSError, RuntimeError):
        return False
    if actual != expected or actual.name != workspace_id:
        return False
    linked_git_dir = linked_worktree_git_dir(actual)
    mirror_path = mirror_path_for_worktree(actual)
    if linked_git_dir is None or mirror_path is None or not mirror_path.is_dir():
        return False
    try:
        registered_worktree = linked_worktree_path_from_git_dir(linked_git_dir)
    except GitOperationError:
        return False
    return registered_worktree == actual


async def _abandon_unpublished_comment_repairs(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    remote_branch: str,
    expected_remote_head: str,
    local_head: str,
    state: MonitorState,
    remote_push_url: str | None = None,
) -> tuple[str, _GitPushResult | None]:
    """Reset interrupted, unpublished repair commits to the fetched PR head.

    This is intentionally provenance-only recovery: it never interprets prior
    agent stdout or commit contents. A preserved protected-scope transaction and
    a workflow-scope-blocked repair are excluded because their local commits are
    intentional operator-facing state awaiting a later push retry.
    """
    current_head = local_head.strip()
    if state.has_preserved_protected_block or state.awaiting_workflow_scope:
        return current_head, None

    # Hosted execution and unit seams can legitimately operate without a local
    # linked worktree. The ordinary start-HEAD guard remains authoritative for
    # those paths; rollback is only meaningful for a concrete AWF-linked
    # checkout with Git metadata to verify.
    if not worktree_path.exists() or not (worktree_path / ".git").exists():
        return current_head, None

    def failure(
        reason_code: str,
        message: str,
        **details: object,
    ) -> tuple[str, _GitPushResult]:
        return (
            current_head,
            _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=message,
                reason_code=reason_code,
                details={"phase": "comment_repair_recovery", "pushed": False, **details},
            ),
        )

    try:
        expected_worktree = (self._worktrees_root / workspace_id).resolve()
        actual_worktree = worktree_path.resolve()
    except (OSError, RuntimeError):
        expected_worktree = Path()
        actual_worktree = Path("invalid")
    if actual_worktree != expected_worktree or actual_worktree.name != workspace_id:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the AWF-managed comment-repair worktree; refusing to reset it.",
        )
    expected_head = expected_remote_head.strip()
    if not current_head or not expected_head:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the local and expected PR heads; refusing to reset.",
            local_head=current_head,
            expected_remote_head=expected_head,
        )
    if current_head.lower() == expected_head.lower():
        return current_head, None

    if not _verified_awf_comment_repair_worktree(
        runner=self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    ):
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not verify the AWF-managed comment-repair Git layout; refusing to reset it.",
        )
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="comment_repair_recovery",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        return failure(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
            "Could not repair comment-repair worktree ownership before recovery.",
        )

    fetch = await self._remote_branch_fetch_once(
        worktree_path=worktree_path,
        remote=remote_push_url or "origin",
        remote_branch=remote_branch,
    )
    if not fetch.ok:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Could not fetch the remote PR branch; refusing to reset local repairs.",
            fetch_returncode=fetch.returncode,
            fetch_stderr=fetch.stderr[:400],
        )
    fetched_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", "FETCH_HEAD"),
        env=git_env_without_object_lookup_overrides(),
    )
    fetched_head = fetched_result.stdout.strip()
    if not fetched_result.ok or not fetched_head or fetched_head.lower() != expected_head.lower():
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Fetched PR head did not match the monitor snapshot; refusing to reset.",
            local_head=current_head,
            expected_remote_head=expected_head,
            fetched_remote_head=fetched_head,
        )

    descendant = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            "FETCH_HEAD",
            "HEAD",
        ),
        env=git_env_without_object_lookup_overrides(),
    )
    if not descendant.ok:
        behind = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                "HEAD",
                "FETCH_HEAD",
            ),
            env=git_env_without_object_lookup_overrides(),
        )
        if behind.ok:
            reset = await self._deps.runner.run(
                git_worktree_command(worktree_path, "reset", "--hard", "FETCH_HEAD"),
                env=git_env_without_object_lookup_overrides(),
            )
            if not reset.ok:
                return failure(
                    _COMMENT_REPAIR_ROLLBACK_FAILED,
                    "Could not fast-forward a lagging comment-repair worktree to the remote PR head.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                    reset_stderr=reset.stderr[:400],
                )
            verified = await self._deps.runner.run(
                git_worktree_command(worktree_path, "rev-parse", "HEAD"),
                env=git_env_without_object_lookup_overrides(),
            )
            clean = await self._deps.runner.run(
                git_worktree_command(worktree_path, "status", "--porcelain", "-z"),
                env=git_env_without_object_lookup_overrides(),
            )
            if (
                not verified.ok
                or verified.stdout.strip().lower() != fetched_head.lower()
                or not clean.ok
                or bool(clean.stdout)
            ):
                return failure(
                    _COMMENT_REPAIR_ROLLBACK_FAILED,
                    "Could not verify a lagging comment-repair worktree after fast-forward.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                    verified_head=verified.stdout.strip(),
                )
            return fetched_head, None

        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Local comment-repair HEAD is not a verified descendant of the remote PR head; "
            "refusing to reset.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
        )

    delta_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "--name-status",
            "-z",
            "FETCH_HEAD..HEAD",
        ),
        env=git_env_without_object_lookup_overrides(),
    )
    if not delta_result.ok:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not record the unpublished repair delta; refusing to reset.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
            diff_stderr=delta_result.stderr[:400],
        )
    try:
        abandoned_paths = _changed_paths_from_name_status_z(delta_result.stdout)
    except ProtectedScopeDiffError as exc:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not parse the unpublished repair delta; refusing to reset.",
            local_head=current_head,
            fetched_remote_head=fetched_head,
            diff_error=str(exc),
        )

    reset = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", "FETCH_HEAD"),
        env=git_env_without_object_lookup_overrides(),
    )
    if not reset.ok:
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Could not reset interrupted comment repairs to the remote PR head.",
            abandoned_local_head=current_head,
            fetched_remote_head=fetched_head,
            abandoned_paths=list(abandoned_paths),
            reset_stderr=reset.stderr[:400],
        )
    verified = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", "HEAD"),
        env=git_env_without_object_lookup_overrides(),
    )
    clean = await self._deps.runner.run(
        git_worktree_command(worktree_path, "status", "--porcelain", "-z"),
        env=git_env_without_object_lookup_overrides(),
    )
    if (
        not verified.ok
        or verified.stdout.strip().lower() != fetched_head.lower()
        or not clean.ok
        or bool(clean.stdout)
    ):
        return failure(
            _COMMENT_REPAIR_ROLLBACK_FAILED,
            "Interrupted comment-repair rollback could not be verified clean.",
            abandoned_local_head=current_head,
            fetched_remote_head=fetched_head,
            verified_head=verified.stdout.strip(),
            abandoned_paths=list(abandoned_paths),
        )

    event_payload = {
        "abandoned_local_head": current_head,
        "restored_remote_head": fetched_head,
        "abandoned_paths": list(abandoned_paths),
        "rollback_strategy": "git_reset_hard_to_verified_remote_pr_head",
        "pushed": False,
    }
    append_events = getattr(self, "_append_workspace_events", None)
    if callable(append_events):
        from awf.db.repositories import WorkspaceEventCreate

        await append_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="monitor.comment_repair_unpublished_abandoned",
                    reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
                    payload=event_payload,
                )
            ],
        )
    _log.warning(
        "monitor.comment_repair_unpublished_abandoned",
        workspace_id=workspace_id,
        reason_code=_COMMENT_REPAIR_UNPUBLISHED_ABANDONED,
        **event_payload,
    )
    return fetched_head, None
