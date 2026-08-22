"""Provider-neutral CLI verdict and evidence operations for PR comments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


_log = get_logger(__name__)

AGENT_VERDICT_PROTOCOL_VIOLATION = "AGENT_VERDICT_PROTOCOL_VIOLATION"
AGENT_FIXED_WITHOUT_EVIDENCE = "AGENT_FIXED_WITHOUT_EVIDENCE"

AgentVerdict = Literal["fix_committed", "false_positive", "defer", "needs_human"]
MonitorVerdict = Literal[
    "fix_committed",
    "false_positive",
    "defer",
    "needs_human",
    "agent_failed",
]
# Existing comment-state helpers consume the wider monitor value. Agent-produced
# results remain the narrower ``AgentVerdict`` below.
Verdict = MonitorVerdict


class AgentVerdictProtocolError(ValueError):
    """A safe, typed failure to satisfy or substantiate the verdict protocol."""

    def __init__(
        self,
        *,
        reason_code: str = AGENT_VERDICT_PROTOCOL_VIOLATION,
        message: str = "Agent output did not satisfy the AWF verdict protocol.",
    ) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class AgentVerdictExecutionError(RuntimeError):
    """Provider execution ended without a semantic agent verdict."""

    def __init__(self, *, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("Agent execution ended before AWF accepted a verdict.")


@dataclass(frozen=True)
class VerdictResult:
    verdict: AgentVerdict
    reason: str | None = None


@dataclass(frozen=True)
class MonitorVerdictResult:
    """Wider persisted monitor state for provider failures outside the protocol."""

    verdict: MonitorVerdict
    reason: str | None = None


_VERDICT_PROTOCOL_CORRECTION_SUFFIX = """

Your previous response did not satisfy AWF's machine-readable verdict protocol.
Complete the same review item. Then emit exactly one of these records as the
final non-empty stdout line, with a non-empty reason and no output after it:
AWF-VERDICT: FIXED: <reason>
AWF-VERDICT: FALSE POSITIVE: <reason>
AWF-VERDICT: DEFER: <reason>
AWF-VERDICT: NEEDS_HUMAN: <reason>
Do not decorate, indent, quote, fence, or otherwise wrap the record. Exit
immediately after emitting it.
""".rstrip()

_FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT = (
    "Your previous FIXED record could not be accepted because this review item "
    "made no new item-scoped Git change after its start commit. Do not repeat "
    "FIXED unless you make a contentful change for this item. If the issue is a "
    "duplicate or was already addressed by an earlier review item or commit, "
    "choose FALSE POSITIVE and state that reason."
)


async def _owned_paths_for_prompt(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    session_context = runner._deps.session_factory()
    async with session_context as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return list(workspace.owned_paths) if workspace is not None else []


async def _owned_paths_for_prompt_or_empty(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    try:
        return await _owned_paths_for_prompt(runner, workspace_id)
    except Exception as exc:
        _log.warning(
            "monitor.owned_paths_prompt_unavailable",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error=redact_audit_text(str(exc), limit=240),
        )
        return []


async def _invoke_cli_for_verdict(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> AgentVerdict:
    return cast(
        AgentVerdict,
        (
            await runner._invoke_cli_for_verdict_result(
                workspace_id=workspace_id,
                prompt=prompt,
                commit_message=commit_message,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        ).verdict,
    )


async def _invoke_cli_for_verdict_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    commit_dirty_changes: bool = True,
    require_fix_evidence: bool = True,
    evidence_item_id: str | None = None,
    evidence_body_hash: str | None = None,
    evidence_item_path: str | None = None,
) -> VerdictResult:
    """Run one logical item with at most one protocol-correction attempt.

    Provider execution/recovery errors are outside the protocol retry budget.
    Both protocol attempts share the item-start HEAD. FIXED evidence is
    recomputed from the final candidate HEAD after each attempt, not OR-
    accumulated across attempts, so a correction retry that reverts an
    unaccepted first-attempt commit cannot inherit stale evidence. Any
    corrected non-FIXED verdict, and any provider execution failure before an
    accepted verdict, rolls those unaccepted edits back first.
    ``evidence_item_id`` and ``evidence_body_hash`` remain accepted at the API
    boundary for call-site compatibility; no evidence is persisted or salvaged
    across process restarts.
    """
    del evidence_item_id, evidence_body_hash
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _normalize_evidence_item_path,
    )

    worktree_path = runner._worktrees_root / workspace_id
    item_path = _normalize_evidence_item_path(evidence_item_path or "") or None
    item_start_head = (operation_start_head or "").strip() or None
    command_evidence: list[str] = []

    rev_parse_head = getattr(runner, "_rev_parse_head", None)
    if item_start_head is None and worktree_path.exists() and callable(rev_parse_head):
        item_start_head = await rev_parse_head(worktree_path)

    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )

    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        await _repair_mirror_hooks_or_raise(
            workspace_id=workspace_id,
            mirror_path=mirror_path,
            stage="before_comment_agent",
        )

    logical_fix_evidence = False
    current_prompt = prompt
    item_start_last_push_sha: str | None = None
    if state is not None:
        item_start_last_push_sha = state.last_push_sha
        state.hosted_terminal_head_advanced = False

    for protocol_attempt in range(2):
        dirty_changes_committed = False
        try:
            if await runner._provider_recovery_suppresses_cli(workspace_id):
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_provider_recovery_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=("Could not roll back unaccepted edits after provider recovery."),
                    )
                raise ProviderRecoveryRetryError()

            try:
                result = await runner._run_monitor_agent_with_service_recovery(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=current_prompt,
                    log_source="recovery",
                    command_evidence=command_evidence,
                    operation_start_head=item_start_head,
                    state=state,
                )
            except AgentRunError as exc:
                append_command_evidence(
                    command_evidence,
                    stdout=exc.result.stdout,
                    stderr=exc.result.stderr,
                )
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_provider_failure_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        reason_code=exc.reason_code,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=("Could not roll back unaccepted edits after provider failure."),
                    ) from exc
                await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
                raise AgentVerdictExecutionError(reason_code=exc.reason_code) from exc
            except ProviderRecoveryRetryError as exc:
                # ``_run_monitor_agent_with_service_recovery`` can raise this from its
                # post-restart pre-launch guard after the agent already edited or
                # self-committed. Roll back before propagating so unaccepted residue
                # does not wedge the dirty-worktree gate on the next pass.
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_in_run_provider_recovery_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=("Could not roll back unaccepted edits after provider recovery."),
                    ) from exc
                raise
            except (
                _MonitorAgentServiceRecoverySupersededError,
                _MonitorAgentServiceRecoveryFailedError,
                _MonitorAgentRuntimeOwnershipRepairFailedError,
                _MonitorHeadObjectMissingError,
                _MonitorMirrorHooksPathRepairFailedError,
            ) as exc:
                # ``_run_monitor_agent_with_service_recovery`` can raise these after the
                # agent already edited or self-committed. Roll back before propagating
                # so unaccepted residue does not wedge remonitor or get pushed later.
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_service_recovery_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                        exc_type=type(exc).__name__,
                    )
                    # Infrastructure exits carry terminal reason codes that fix_cycle
                    # handles directly; do not mask them behind protocol violation.
                    if isinstance(
                        exc,
                        (
                            _MonitorAgentRuntimeOwnershipRepairFailedError,
                            _MonitorHeadObjectMissingError,
                            _MonitorMirrorHooksPathRepairFailedError,
                        ),
                    ):
                        raise
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after service recovery exit."
                        ),
                    ) from exc
                raise
            except Exception:
                if mirror_path is not None:
                    await _repair_mirror_hooks_or_raise(
                        workspace_id=workspace_id,
                        mirror_path=mirror_path,
                        stage="after_comment_agent_exception",
                    )
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_unexpected_failure_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                    )
                raise

            try:
                if commit_dirty_changes:
                    dirty_changes_committed = await runner._commit_dirty_worktree(
                        workspace_id=workspace_id,
                        message=commit_message,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        state=state,
                        command_evidence=command_evidence,
                        task_tag=task_tag,
                        operation_start_head=item_start_head,
                    )

                logical_fix_evidence = await _item_fix_evidence(
                    runner,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_path=item_path,
                    state=state,
                    dirty_changes_committed=dirty_changes_committed,
                )
            except (
                ProviderRecoveryRetryError,
                ProviderRecoveryFallbackError,
                ProviderRecoveryAuthError,
                _MonitorAgentServiceRecoverySupersededError,
                _MonitorAgentServiceRecoveryFailedError,
                _MonitorAgentRuntimeOwnershipRepairFailedError,
                _MonitorHeadObjectMissingError,
                _MonitorMirrorHooksPathRepairFailedError,
            ) as exc:
                # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
                # raises these when provider recovery suppresses the CLI, a recoverable
                # agent-run error triggers retry/fallback/auth, or infrastructure exits
                # (service-recovery, ownership, head-object, mirror-hook) occur before or
                # during the sink's nested protected-scope repair. Roll back before
                # propagating so unaccepted residue does not wedge remonitor or get pushed
                # later.
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    _log.warning(
                        "monitor.agent_verdict_commit_sink_infrastructure_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                        exc_type=type(exc).__name__,
                    )
                    # Infrastructure exits carry terminal reason codes that fix_cycle
                    # handles directly; do not mask them behind protocol violation.
                    if isinstance(
                        exc,
                        (
                            _MonitorAgentRuntimeOwnershipRepairFailedError,
                            _MonitorHeadObjectMissingError,
                            _MonitorMirrorHooksPathRepairFailedError,
                        ),
                    ):
                        raise
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after commit sink "
                            "infrastructure exit."
                        ),
                    ) from exc
                raise

            protocol_error: AgentVerdictProtocolError | None = None
            try:
                parsed = _parse_verdict_result(result.stdout)
            except AgentVerdictProtocolError as exc:
                protocol_error = exc
            else:
                if (
                    parsed.verdict == "fix_committed"
                    and require_fix_evidence
                    and not logical_fix_evidence
                ):
                    protocol_error = AgentVerdictProtocolError(
                        reason_code=AGENT_FIXED_WITHOUT_EVIDENCE,
                        message="Agent reported FIXED without item-scoped Git evidence.",
                    )
                else:
                    if parsed.verdict != "fix_committed":
                        rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                            runner,
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                            item_start_head=item_start_head,
                            item_start_last_push_sha=item_start_last_push_sha,
                            state=state,
                        )
                        if not rollback_ok:
                            raise AgentVerdictProtocolError(
                                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                message=(
                                    "Could not roll back unaccepted edits before "
                                    "accepting a non-FIXED verdict."
                                ),
                            )
                    return parsed

            assert protocol_error is not None
            if protocol_attempt == 1:
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                )
                if not rollback_ok:
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits before "
                            "terminating after protocol violation."
                        ),
                    )
                raise protocol_error
            _log.warning(
                "monitor.agent_verdict_protocol_retry",
                workspace_id=workspace_id,
                reason_code=protocol_error.reason_code,
            )
            correction_context = (
                f"\n\n{_FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT}"
                if protocol_error.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
                else ""
            )
            current_prompt = f"{prompt}{correction_context}{_VERDICT_PROTOCOL_CORRECTION_SUFFIX}"
        except asyncio.CancelledError:
            # ``CancelledError`` is a ``BaseException`` and bypasses ``except
            # Exception``. Roll back agent edits/self-commits before re-raising so
            # unaccepted residue cannot be pushed on a later repair cycle.
            rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                item_start_head=item_start_head,
                item_start_last_push_sha=item_start_last_push_sha,
                state=state,
            )
            if not rollback_ok:
                _log.warning(
                    "monitor.agent_verdict_cancellation_rollback_failed",
                    workspace_id=workspace_id,
                    item_start_head=item_start_head,
                    protocol_attempt=protocol_attempt,
                )
                raise AgentVerdictProtocolError(
                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                    message=(
                        "Could not roll back unaccepted edits before "
                        "terminating after worker cancellation."
                    ),
                ) from None
            raise

    raise AssertionError("unreachable verdict retry state")


async def _rollback_unaccepted_protocol_retry_changes(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    item_start_last_push_sha: str | None = None,
    state: MonitorState | None,
) -> bool:
    """Discard first-attempt edits when a corrected verdict is not FIXED.

    When HEAD still equals ``item_start_head``, uncommitted agent edits are
    cleaned via ``cleanup_validation_worktree_side_effects`` so they cannot
    contaminate the next review item in the same cycle.

    Returns ``True`` when rollback succeeded or was unnecessary, and ``False``
    when ``git reset --hard`` or cleanup/verification failed so the caller must
    not accept the verdict.
    """
    if item_start_head is None or not worktree_path.exists():
        return True

    rev_parse_head = getattr(runner, "_rev_parse_head", None)
    if not callable(rev_parse_head):
        return True

    current_head = await rev_parse_head(worktree_path)
    if not current_head:
        _log.warning(
            "monitor.agent_verdict_protocol_retry_rollback_head_unreadable",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
        )
        return False

    needs_hosted_remote_rollback = False
    published_remote_head: str | None = None
    if state is not None and getattr(runner._deps.adapter, "is_hosted", False):
        saved_last_push_sha = (item_start_last_push_sha or "").strip()
        current_last_push_sha = (state.last_push_sha or "").strip()
        start_head_lower = item_start_head.lower()
        if state.hosted_terminal_head_advanced or (
            current_last_push_sha.lower() != saved_last_push_sha.lower()
        ):
            needs_hosted_remote_rollback = True
        if needs_hosted_remote_rollback:
            candidate = current_last_push_sha or current_head
            if candidate and candidate.lower() != start_head_lower:
                published_remote_head = candidate
            else:
                needs_hosted_remote_rollback = False

    head_matches_start = current_head.lower() == item_start_head.lower()
    rolled_back_from: str | None = None
    if not head_matches_start:
        reset = await runner._deps.runner.run(
            git_worktree_command(worktree_path, "reset", "--hard", item_start_head)
        )
        if not reset.ok:
            _log.warning(
                "monitor.agent_verdict_protocol_retry_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                current_head=current_head,
                reset_returncode=reset.returncode,
                reset_stderr=(reset.stderr or "")[:400],
            )
            return False
        rolled_back_from = current_head

    async def _run_git(args: list[str]) -> CommandResult:
        return await runner._deps.runner.run(git_worktree_command(worktree_path, *args))

    from awf.runtime.validation_worktree import cleanup_validation_worktree_side_effects

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=_run_git,
        worktree_path=worktree_path,
        restore_ref=item_start_head,
    )
    if not cleanup.ok:
        _log.warning(
            "monitor.agent_verdict_protocol_retry_rollback_cleanup_failed",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
            reason_code=cleanup.reason_code,
            cleanup_stderr=(cleanup.cleanup_stderr or "")[:400],
        )
        return False

    if needs_hosted_remote_rollback and published_remote_head is not None:
        hosted_identity_fn = getattr(runner, "_hosted_pr_identity_for_workspace", None)
        if not callable(hosted_identity_fn):
            _log.warning(
                "monitor.hosted_terminal_head_remote_rollback_unavailable",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                published_remote_head=published_remote_head,
            )
            return False
        hosted_pr_identity = await hosted_identity_fn(workspace_id, state=state)
        from awf.runtime.pr_monitor_runner.agent_service_recovery import (
            _rollback_hosted_terminal_head_on_remote,
        )

        remote_ok = await _rollback_hosted_terminal_head_on_remote(
            runner,
            workspace_id=workspace_id,
            hosted_pr_identity=hosted_pr_identity,
            rollback_target_sha=item_start_head,
            expected_remote_head_sha=published_remote_head,
        )
        if not remote_ok:
            return False

    if state is not None:
        state.hosted_terminal_head_advanced = False
        current_last_push_sha = (state.last_push_sha or "").strip()
        saved_last_push_sha = (item_start_last_push_sha or "").strip()
        if current_last_push_sha.lower() != saved_last_push_sha.lower():
            state.last_push_sha = item_start_last_push_sha

    if rolled_back_from is not None or not cleanup.check.clean:
        _log.info(
            "monitor.agent_verdict_protocol_retry_rollback",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
            rolled_back_from=rolled_back_from,
            verdict_outcome="non_fix",
            cleaned_paths=list(cleanup.cleaned_paths),
        )
    return True


async def _repair_mirror_hooks_or_raise(
    *,
    workspace_id: str,
    mirror_path: Path,
    stage: str,
) -> None:
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        details = mirror_hooks_repair_failure_details(
            exc,
            repair_stage=stage,
            mirror_path=mirror_path,
        )
        _log.warning(
            "monitor.mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            **details,
        )
        raise _MonitorMirrorHooksPathRepairFailedError() from exc


async def _item_fix_evidence(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
    item_path: str | None,
    state: MonitorState | None,
    dirty_changes_committed: bool,
) -> bool:
    """Verify a contentful forward item-scoped change from the logical start."""
    if item_start_head is None:
        return dirty_changes_committed and not worktree_path.exists()

    candidate_heads: list[str] = []
    if worktree_path.exists():
        end_head = await runner._rev_parse_head(worktree_path)
        if end_head:
            candidate_heads.append(end_head)
    if state is not None and state.hosted_terminal_head_advanced:
        hosted_head = (state.last_push_sha or "").strip()
        if hosted_head and hosted_head not in candidate_heads:
            candidate_heads.append(hosted_head)

    descends = getattr(runner, "_head_descends_from", None)
    trees_differ = getattr(runner, "_commit_trees_differ", None)
    in_item_scope = getattr(runner, "_commit_range_in_item_scope", None)
    if not (callable(descends) and callable(trees_differ) and worktree_path.exists()):
        # Lightweight/mocked runners may not expose Git ancestry helpers. A
        # successful dirty-worktree sink is still scoped to this invocation;
        # the production runner always takes the stronger ancestry/scope branch.
        return dirty_changes_committed and not worktree_path.exists()

    for candidate in candidate_heads:
        if candidate.lower() == item_start_head.lower():
            continue
        if not await descends(
            worktree_path=worktree_path,
            ancestor=item_start_head,
            descendant=candidate,
        ):
            continue
        if not await trees_differ(
            worktree_path=worktree_path,
            left=item_start_head,
            right=candidate,
        ):
            continue
        if item_path is not None and (
            not callable(in_item_scope)
            or not await in_item_scope(
                worktree_path=worktree_path,
                left=item_start_head,
                right=candidate,
                item_path=item_path,
            )
        ):
            continue
        return True
    return False
