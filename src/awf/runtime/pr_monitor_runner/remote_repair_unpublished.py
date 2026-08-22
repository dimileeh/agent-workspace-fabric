"""Recovery for interrupted, unpublished PR comment repairs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation
from awf.db.repositories import OperationRepository
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

_OPERATOR_HINT_REPAIR_ACTION = "operator_hint_repair"
_NON_COMMENT_REPAIR_UNPUBLISHED_TYPES = frozenset(
    {
        OperationType.ci_repair.value,
        OperationType.sync_base.value,
    }
)
_UNPUBLISHED_REPAIR_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.pending.value,
        OperationStatus.running.value,
        OperationStatus.failed.value,
        OperationStatus.cancelled.value,
    }
)


def _operation_payload_source_head_sha(operation: Operation) -> str | None:
    payload = operation.payload
    if not isinstance(payload, dict):
        return None
    source_head = payload.get("source_head_sha")
    if isinstance(source_head, str) and source_head.strip():
        return source_head.strip()
    return None


def _operation_payload_action(operation: Operation) -> str | None:
    payload = operation.payload
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if isinstance(action, str) and action.strip():
        return action.strip()
    return None


def _is_operator_hint_repair_operation(operation: Operation) -> bool:
    return (
        operation.type == OperationType.comment_repair.value
        and _operation_payload_action(operation) == _OPERATOR_HINT_REPAIR_ACTION
    )


def _operation_result_was_pushed(operation: Operation) -> bool:
    if operation.status != OperationStatus.succeeded.value:
        return False
    result = operation.result
    if not isinstance(result, dict):
        return False
    if result.get("pushed"):
        return True
    outcome = result.get("outcome")
    return isinstance(outcome, str) and outcome.endswith("_pushed")


def _operation_mapping_head_sha(
    mapping: Mapping[str, Any] | None,
    keys: tuple[str, ...],
) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _operation_recorded_local_terminal_head(operation: Operation) -> str | None:
    result = operation.result
    if isinstance(result, dict):
        terminal = _operation_mapping_head_sha(
            result,
            ("local_terminal_head_sha", "terminal_head_sha"),
        )
        if terminal is not None:
            return terminal
        recovery = result.get("agent_service_recovery")
        if isinstance(recovery, dict):
            terminal = _operation_mapping_head_sha(
                recovery,
                ("terminal_head_sha", "local_terminal_head_sha"),
            )
            if terminal is not None:
                return terminal
        evidence = result.get("failure_evidence")
        if isinstance(evidence, dict):
            terminal = _operation_mapping_head_sha(
                evidence,
                ("local_terminal_head_sha", "terminal_head_sha", "head_sha"),
            )
            if terminal is not None:
                return terminal
    payload = operation.payload
    if isinstance(payload, dict):
        return _operation_mapping_head_sha(
            payload,
            ("local_terminal_head_sha", "terminal_head_sha"),
        )
    return None


def _operation_owns_discarded_commits(
    operation: Operation,
    *,
    remote_pr_head: str,
    discarded_local_head: str,
) -> bool:
    """Return whether the operation produced the unpushed commits being abandoned."""
    source_head = _operation_payload_source_head_sha(operation)
    discarded = discarded_local_head.strip()
    remote = remote_pr_head.strip()
    if not source_head or not discarded or not remote:
        return False
    if source_head.lower() != remote.lower():
        return False
    if discarded.lower() == source_head.lower():
        return False

    terminal_head = _operation_recorded_local_terminal_head(operation)
    if terminal_head is not None:
        if terminal_head.lower() == source_head.lower():
            return False
        return discarded.lower() == terminal_head.lower()

    return False


def _is_active_unpublished_repair_operation(operation: Operation) -> bool:
    if operation.status not in _UNPUBLISHED_REPAIR_OPERATION_STATUSES:
        return False
    return not _operation_result_was_pushed(operation)


async def _unpublished_comment_repair_has_operation_provenance(
    runner: Any,
    *,
    workspace_id: str,
    remote_pr_head: str,
    discarded_local_head: str,
    exclude_operation_id: str | None = None,
) -> bool:
    """Return whether a prior comment-repair operation owns unpushed local commits."""
    session_factory = getattr(runner._deps, "session_factory", None)
    if session_factory is None:
        return False
    async with session_factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            operation_type=OperationType.comment_repair.value,
            limit=100,
        )
    for operation in operations:
        if exclude_operation_id and operation.id == exclude_operation_id:
            continue
        if _is_operator_hint_repair_operation(operation):
            continue
        if not _is_active_unpublished_repair_operation(operation):
            continue
        if _operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_pr_head,
            discarded_local_head=discarded_local_head,
        ):
            return True
    return False


async def _unpublished_non_comment_repair_has_operation_provenance(
    runner: Any,
    *,
    workspace_id: str,
    remote_pr_head: str,
    discarded_local_head: str,
) -> bool:
    """Return whether another repair path owns unpushed commits at the PR head."""
    session_factory = getattr(runner._deps, "session_factory", None)
    if session_factory is None:
        return False
    async with session_factory() as session:
        operations = await OperationRepository(session).list_for_workspace(
            workspace_id,
            limit=100,
        )
    for operation in operations:
        if (
            operation.type not in _NON_COMMENT_REPAIR_UNPUBLISHED_TYPES
            and not _is_operator_hint_repair_operation(operation)
        ):
            continue
        if not _is_active_unpublished_repair_operation(operation):
            continue
        if _operation_owns_discarded_commits(
            operation,
            remote_pr_head=remote_pr_head,
            discarded_local_head=discarded_local_head,
        ):
            return True
    return False


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
    current_operation_id: str | None = None,
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
    if not fetched_result.ok or not fetched_head:
        return failure(
            _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
            "Fetched PR head did not match the monitor snapshot; refusing to reset.",
            local_head=current_head,
            expected_remote_head=expected_head,
            fetched_remote_head=fetched_head,
        )
    stale_snapshot_advance = False
    if fetched_head.lower() != expected_head.lower():
        # A successful monitor-owned push can advance both local and remote HEAD
        # before the next forge status snapshot refreshes. Accept only the exact
        # already-published local HEAD and prove it advances the stale snapshot;
        # every divergent or rewritten remote still fails closed below.
        if current_head.lower() == fetched_head.lower():
            published_descendant = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    "FETCH_HEAD",
                ),
                env=git_env_without_object_lookup_overrides(),
            )
            if published_descendant.ok:
                return fetched_head, None
        stale_snapshot = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                expected_head,
                "FETCH_HEAD",
            ),
            env=git_env_without_object_lookup_overrides(),
        )
        if not stale_snapshot.ok:
            return failure(
                _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                "Fetched PR head did not match the monitor snapshot; refusing to reset.",
                local_head=current_head,
                expected_remote_head=expected_head,
                fetched_remote_head=fetched_head,
            )
        stale_snapshot_advance = True

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
    use_stale_snapshot_diff = False
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

        if stale_snapshot_advance:
            on_stale_snapshot_base = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "merge-base",
                    "--is-ancestor",
                    expected_head,
                    "HEAD",
                ),
                env=git_env_without_object_lookup_overrides(),
            )
            if not on_stale_snapshot_base.ok:
                return failure(
                    _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                    "Local comment-repair HEAD is not a verified descendant of the remote PR head; "
                    "refusing to reset.",
                    local_head=current_head,
                    fetched_remote_head=fetched_head,
                )
            use_stale_snapshot_diff = True
        else:
            return failure(
                _COMMENT_REPAIR_REMOTE_HEAD_VERIFICATION_FAILED,
                "Local comment-repair HEAD is not a verified descendant of the remote PR head; "
                "refusing to reset.",
                local_head=current_head,
                fetched_remote_head=fetched_head,
            )

    diff_range = f"{expected_head}..HEAD" if use_stale_snapshot_diff else "FETCH_HEAD..HEAD"
    delta_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "--name-status",
            "-z",
            diff_range,
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

    has_comment_repair_provenance = await _unpublished_comment_repair_has_operation_provenance(
        self,
        workspace_id=workspace_id,
        remote_pr_head=fetched_head,
        discarded_local_head=current_head,
        exclude_operation_id=current_operation_id,
    )
    has_conflicting_repair_provenance = (
        await _unpublished_non_comment_repair_has_operation_provenance(
            self,
            workspace_id=workspace_id,
            remote_pr_head=fetched_head,
            discarded_local_head=current_head,
        )
    )
    if not has_comment_repair_provenance or has_conflicting_repair_provenance:
        _log.info(
            "monitor.comment_repair_unpublished_reset_skipped_missing_provenance",
            workspace_id=workspace_id,
            local_head=current_head,
            fetched_remote_head=fetched_head,
            has_comment_repair_provenance=has_comment_repair_provenance,
            has_conflicting_repair_provenance=has_conflicting_repair_provenance,
            current_operation_id=current_operation_id,
        )
        return current_head, None

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
