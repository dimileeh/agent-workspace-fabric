"""Provider-neutral CLI verdict and evidence operations for PR comments."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
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
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

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
    evidence_item_line: int | None = None,
    evidence_anchor_head: str | None = None,
) -> VerdictResult:
    """Run one logical item with at most one protocol-correction attempt.

    Provider execution/recovery errors are outside the protocol retry budget.
    Both protocol attempts share the item-start HEAD. FIXED evidence is
    recomputed from the final candidate HEAD after each attempt, not OR-
    accumulated across attempts, so a correction retry that reverts an
    unaccepted first-attempt commit cannot inherit stale evidence. A corrected
    non-FIXED verdict is accepted only when the correction attempt itself did
    not advance HEAD, commit dirty changes it authored, leave new PR-worthy
    uncommitted residue after a False commit sink, or otherwise mutate relative
    to the HEAD and dirty state at the start of that attempt. Pre-existing
    attempt-0 residue left by a False first sink is not attributed to a clean
    correction: sinking or re-detecting that same residue still rolls back to
    item-start and accepts the verdict (PRRT_kwDOSJAM6s6eKNQT). Mutation plus
    non-FIXED is a protocol violation after safe rollback. First-attempt
    non-FIXED still rolls back unaccepted edits and returns the verdict. Any
    provider execution failure before an accepted verdict also rolls unaccepted
    edits back first.
    ``evidence_item_id`` and ``evidence_body_hash`` remain accepted at the API
    boundary for call-site compatibility; no evidence is persisted or salvaged
    across process restarts.
    """
    del evidence_item_id, evidence_body_hash
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _map_review_line_through_commits,
        _map_review_path_through_commits,
        _normalize_evidence_item_path,
    )

    worktree_path = runner._worktrees_root / workspace_id
    item_path = _normalize_evidence_item_path(evidence_item_path or "") or None
    item_line = evidence_item_line
    item_start_head = (operation_start_head or "").strip() or None
    anchor_head = (evidence_anchor_head or "").strip() or None
    if (
        item_path is not None
        and anchor_head is not None
        and item_start_head is not None
        and anchor_head.lower() != item_start_head.lower()
    ):
        original_item_path = item_path
        mapped_path = await _map_review_path_through_commits(
            runner,
            worktree_path=worktree_path,
            anchor_head=anchor_head,
            target_head=item_start_head,
            path=item_path,
        )
        if mapped_path is None:
            item_line = -1
        else:
            item_path = mapped_path
        if item_line is not None:
            mapped_line = await _map_review_line_through_commits(
                runner,
                worktree_path=worktree_path,
                anchor_head=anchor_head,
                target_head=item_start_head,
                path=original_item_path,
                line=item_line,
            )
            item_line = -1 if mapped_line is None else mapped_line
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
    # Tip verified after attempt 0 (post commit/evidence). Used when
    # correction-start rev-parse fails so we neither retain stale
    # ``item_start_head`` (IM7m) nor clear the baseline and miss correction
    # self-commits (Ij5y). Must not be seeded from attempt-0 *start* HEAD.
    verified_attempt_tip: str | None = None
    # Porcelain fingerprint at correction start so attempt-0 False-sink
    # residue is not attributed to a clean correction (PRRT_kwDOSJAM6s6eKNQT).
    # None means the baseline probe failed (fail closed on mutation signals).
    correction_start_residue_fp: str | None = None
    correction_authored_mutation = False

    for protocol_attempt in range(2):
        dirty_changes_committed = False
        compose_cleanup_error: ComposeExecCleanupError | None = None
        attempt_start_head = item_start_head
        correction_authored_mutation = False
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
                # Correction-/attempt-start HEAD probe must stay inside this
                # guarded region (PRRT_kwDOSJAM6s6eJCpZ): after attempt 0 may
                # have mutated the worktree, cancel or raise while reading HEAD
                # must hit the Exception / CancelledError rollback handlers.
                if worktree_path.exists() and callable(rev_parse_head):
                    parsed_attempt_start = await rev_parse_head(worktree_path)
                    if parsed_attempt_start:
                        attempt_start_head = parsed_attempt_start
                    elif protocol_attempt > 0:
                        # Live correction-start read failed. Do not retain
                        # ``item_start_head``: attempt 0 may already have advanced
                        # HEAD, and a later successful read of that unchanged tip
                        # would be misattributed as correction mutation
                        # (PRRT_kwDOSJAM6s6eIM7m). Carry forward the tip verified
                        # after attempt 0 so a correction self-commit remains
                        # measurable (PRRT_kwDOSJAM6s6eIj5y).
                        attempt_start_head = verified_attempt_tip
                if protocol_attempt > 0:
                    # Capture dirty state before the correction agent so a later
                    # successful sink of attempt-0 residue is not treated as
                    # correction mutation (PRRT_kwDOSJAM6s6eKNQT).
                    correction_start_residue_fp = (
                        await _read_correction_pr_worthy_residue_fingerprint(
                            runner,
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                        )
                    )
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
            except ComposeExecCleanupError as exc:
                # Agent output may exist even when compose cleanup fails. Roll back
                # before mirror repair, then attempt the dirty-worktree sink before
                # re-raising so uncommitted residue cannot block remonitor.
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
                        "monitor.agent_verdict_compose_cleanup_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after compose cleanup failure."
                        ),
                    ) from exc
                if mirror_path is not None:
                    try:
                        await _repair_mirror_hooks_or_raise(
                            workspace_id=workspace_id,
                            mirror_path=mirror_path,
                            stage="after_comment_agent_exception",
                        )
                    except _MonitorMirrorHooksPathRepairFailedError:
                        # Compose cleanup may leave a live agent that re-dirties the
                        # worktree after the rollback above. Roll back again before
                        # propagating hook repair failure so residue cannot block remonitor.
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
                                "monitor.agent_verdict_compose_cleanup_hook_repair_rollback_failed",
                                workspace_id=workspace_id,
                                item_start_head=item_start_head,
                                protocol_attempt=protocol_attempt,
                            )
                            raise AgentVerdictProtocolError(
                                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                message=(
                                    "Could not roll back unaccepted edits after compose "
                                    "cleanup hook repair failure."
                                ),
                            ) from exc
                        raise
                compose_cleanup_error = exc
            except Exception as exc:
                # Roll back before post-exception hook repair so a repair failure
                # cannot strand uncommitted edits that block remonitor.
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
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after unexpected "
                            "invocation failure."
                        ),
                    ) from exc
                if mirror_path is not None:
                    await _repair_mirror_hooks_or_raise(
                        workspace_id=workspace_id,
                        mirror_path=mirror_path,
                        stage="after_comment_agent_exception",
                    )
                raise

            if compose_cleanup_error is not None:
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
                except (
                    ProviderRecoveryRetryError,
                    ProviderRecoveryFallbackError,
                    ProviderRecoveryAuthError,
                    _MonitorAgentServiceRecoverySupersededError,
                    _MonitorAgentServiceRecoveryFailedError,
                    _MonitorAgentRuntimeOwnershipRepairFailedError,
                    _MonitorHeadObjectMissingError,
                    _MonitorMirrorHooksPathRepairFailedError,
                    _MonitorPolicyBlockedError,
                    ProtectedScopeDiffError,
                ) as exc:
                    # Roll back before propagating commit-sink infrastructure exits so
                    # unaccepted residue does not wedge remonitor or get pushed later.
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
                            "monitor.agent_verdict_compose_cleanup_sink_rollback_failed",
                            workspace_id=workspace_id,
                            item_start_head=item_start_head,
                            protocol_attempt=protocol_attempt,
                            exc_type=type(exc).__name__,
                        )
                        if isinstance(
                            exc,
                            (
                                _MonitorAgentRuntimeOwnershipRepairFailedError,
                                _MonitorHeadObjectMissingError,
                                _MonitorMirrorHooksPathRepairFailedError,
                                _MonitorPolicyBlockedError,
                                ProtectedScopeDiffError,
                            ),
                        ):
                            raise
                        raise AgentVerdictProtocolError(
                            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                            message=(
                                "Could not roll back unaccepted edits after compose cleanup "
                                "commit sink infrastructure exit."
                            ),
                        ) from exc
                    raise
                except Exception as exc:
                    # ``_commit_dirty_worktree`` can raise untyped failures (for example
                    # repository/session errors from supply-chain policy refresh) after
                    # the agent has already edited the worktree. Roll back before
                    # propagating so unaccepted residue does not wedge remonitor or
                    # get pushed later.
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
                            "monitor.agent_verdict_compose_cleanup_sink_unexpected_rollback_failed",
                            workspace_id=workspace_id,
                            item_start_head=item_start_head,
                            protocol_attempt=protocol_attempt,
                            exc_type=type(exc).__name__,
                        )
                        raise AgentVerdictProtocolError(
                            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                            message=(
                                "Could not roll back unaccepted edits after unexpected "
                                "compose cleanup commit sink failure."
                            ),
                        ) from exc
                    raise
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
                        "monitor.agent_verdict_compose_cleanup_sink_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after compose cleanup "
                            "commit sink."
                        ),
                    ) from compose_cleanup_error
                raise compose_cleanup_error

            try:
                if protocol_attempt > 0:
                    # Measure correction-authored mutation before the sink so a
                    # successful commit of pre-existing attempt-0 residue is not
                    # counted as correction mutation (PRRT_kwDOSJAM6s6eKNQT).
                    # Unreadable pre-sink HEAD must fail closed (None), not keep
                    # attempt_start_head: a correction self-commit would look
                    # unchanged and the later residue-sink gate would accept
                    # non-FIXED (PRRT_kwDOSJAM6s6eKoIe).
                    pre_sink_head: str | None = attempt_start_head
                    if worktree_path.exists() and callable(rev_parse_head):
                        try:
                            live_pre_sink = await rev_parse_head(worktree_path)
                        except Exception:
                            live_pre_sink = None
                        pre_sink_head = live_pre_sink if live_pre_sink else None
                    pre_sink_residue_fp = await _read_correction_pr_worthy_residue_fingerprint(
                        runner,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                    )
                    correction_authored_mutation = _correction_authored_mutation_vs_start(
                        attempt_start_head=attempt_start_head,
                        pre_sink_head=pre_sink_head,
                        correction_start_residue_fp=correction_start_residue_fp,
                        pre_sink_residue_fp=pre_sink_residue_fp,
                    )
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
                    item_line=item_line,
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
                _MonitorPolicyBlockedError,
                ProtectedScopeDiffError,
            ) as exc:
                # ``_commit_dirty_worktree`` / ``_item_fix_evidence`` raise these when
                # provider recovery suppresses the CLI, a recoverable agent-run error
                # triggers retry/fallback/auth, infrastructure exits (service-recovery,
                # ownership, head-object, mirror-hook) occur before or during the sink's
                # nested protected-scope repair, supply-chain policy blocks the commit
                # before ``git commit``, or the protected-scope diff cannot be verified.
                # Roll back before propagating so unaccepted residue does not wedge
                # remonitor or get pushed later.
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
                    # Infrastructure exits, policy-blocked, and protected-scope diff
                    # failures carry reason codes that fix_cycle handles directly; do
                    # not mask them behind protocol violation.
                    if isinstance(
                        exc,
                        (
                            _MonitorAgentRuntimeOwnershipRepairFailedError,
                            _MonitorHeadObjectMissingError,
                            _MonitorMirrorHooksPathRepairFailedError,
                            _MonitorPolicyBlockedError,
                            ProtectedScopeDiffError,
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
            except Exception as exc:
                # ``_commit_dirty_worktree`` / ``_item_fix_evidence`` can raise untyped
                # failures (for example repository/session errors from supply-chain
                # policy refresh) after the agent has already edited the worktree. Roll
                # back before propagating so unaccepted residue does not wedge remonitor
                # or get pushed later.
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
                        "monitor.agent_verdict_commit_sink_unexpected_rollback_failed",
                        workspace_id=workspace_id,
                        item_start_head=item_start_head,
                        protocol_attempt=protocol_attempt,
                        exc_type=type(exc).__name__,
                    )
                    raise AgentVerdictProtocolError(
                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                        message=(
                            "Could not roll back unaccepted edits after unexpected "
                            "commit sink failure."
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
                        if protocol_attempt == 1:
                            if attempt_start_head is None:
                                # Correction baseline unreadable and no tip was
                                # verified after attempt 0 — cannot measure whether
                                # the retry self-committed (PRRT_kwDOSJAM6s6eIj5y).
                                _log.warning(
                                    "monitor.agent_verdict_correction_baseline_unreadable",
                                    workspace_id=workspace_id,
                                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                    protocol_attempt=protocol_attempt,
                                    verdict=parsed.verdict,
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
                                    raise AgentVerdictProtocolError(
                                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                        message=(
                                            "Could not roll back unaccepted edits after "
                                            "correction attempt with unreadable baseline."
                                        ),
                                    )
                                raise AgentVerdictProtocolError(
                                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                    message=(
                                        "Correction attempt baseline was unreadable; "
                                        "cannot accept a non-FIXED verdict without "
                                        "measuring whether the worktree advanced."
                                    ),
                                )
                            post_attempt_head = attempt_start_head
                            if worktree_path.exists() and callable(rev_parse_head):
                                # Correction-end probe must roll back on ordinary
                                # failures (PRRT_kwDOSJAM6s6eJ2Tg): after the
                                # correction attempt may have mutated the
                                # worktree, OSError/RuntimeError while spawning
                                # Git is outside Exception handlers here, and
                                # the surrounding handler catches only
                                # CancelledError. Match the post-attempt tip
                                # probe (PRRT_kwDOSJAM6s6eJUbE).
                                try:
                                    live_head = await rev_parse_head(worktree_path)
                                except Exception as end_head_exc:
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
                                            "monitor.agent_verdict_correction_end_head_rollback_failed",
                                            workspace_id=workspace_id,
                                            item_start_head=item_start_head,
                                            protocol_attempt=protocol_attempt,
                                            exc_type=type(end_head_exc).__name__,
                                        )
                                        raise AgentVerdictProtocolError(
                                            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                            message=(
                                                "Could not roll back unaccepted edits after "
                                                "correction-end HEAD probe failure."
                                            ),
                                        ) from end_head_exc
                                    raise
                                if live_head:
                                    post_attempt_head = live_head
                                else:
                                    # Transient None must not leave
                                    # post_attempt_head == attempt_start_head:
                                    # a clean self-commit would then miss
                                    # mutation, and a later successful
                                    # rollback could accept FALSE POSITIVE /
                                    # DEFER / NEEDS_HUMAN (PRRT_kwDOSJAM6s6eIz5m).
                                    _log.warning(
                                        "monitor.agent_verdict_correction_end_head_unreadable",
                                        workspace_id=workspace_id,
                                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                        protocol_attempt=protocol_attempt,
                                        attempt_start_head=attempt_start_head,
                                        verdict=parsed.verdict,
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
                                        raise AgentVerdictProtocolError(
                                            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                            message=(
                                                "Could not roll back unaccepted edits after "
                                                "correction attempt with unreadable end HEAD."
                                            ),
                                        )
                                    raise AgentVerdictProtocolError(
                                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                        message=(
                                            "Correction attempt end HEAD was unreadable; "
                                            "cannot accept a non-FIXED verdict without "
                                            "measuring whether the worktree advanced."
                                        ),
                                    )
                            head_advanced = (
                                attempt_start_head is not None
                                and post_attempt_head is not None
                                and post_attempt_head.lower() != attempt_start_head.lower()
                            )
                            # Attribute mutation to the correction attempt only.
                            # Attempt-0 False-sink residue may still be dirty at
                            # correction start; a clean correction can sink that
                            # residue (dirty_changes_committed / head_advanced)
                            # or leave the same porcelain after another False
                            # sink without having authored changes
                            # (PRRT_kwDOSJAM6s6eKNQT).
                            stranded_dirty_residue = False
                            if not correction_authored_mutation:
                                if not (dirty_changes_committed or head_advanced):
                                    post_residue_fp = (
                                        await _read_correction_pr_worthy_residue_fingerprint(
                                            runner,
                                            workspace_id=workspace_id,
                                            worktree_path=worktree_path,
                                        )
                                    )
                                    if _stranded_residue_is_correction_mutation(
                                        correction_start_residue_fp=correction_start_residue_fp,
                                        post_residue_fp=post_residue_fp,
                                    ):
                                        stranded_dirty_residue = True
                                        correction_authored_mutation = True
                                elif (
                                    correction_start_residue_fp is None
                                    or correction_start_residue_fp == ""
                                ):
                                    # Clean or unreadable correction-start: a sink
                                    # commit / HEAD advance cannot be attempt-0
                                    # residue — fail closed (pre-sink should have
                                    # caught agent-authored dirt when measurable).
                                    correction_authored_mutation = True
                                # else: start had residue; sinking that residue
                                # (or HEAD advance from that sink alone) is not
                                # correction mutation (PRRT_kwDOSJAM6s6eKNQT).
                            attempt_mutated = correction_authored_mutation
                            if attempt_mutated:
                                _log.warning(
                                    "monitor.agent_verdict_correction_non_fixed_with_mutation",
                                    workspace_id=workspace_id,
                                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                    protocol_attempt=protocol_attempt,
                                    attempt_start_head=attempt_start_head,
                                    current_head=post_attempt_head,
                                    verdict=parsed.verdict,
                                    dirty_changes_committed=dirty_changes_committed,
                                    stranded_dirty_residue=stranded_dirty_residue,
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
                                    raise AgentVerdictProtocolError(
                                        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                        message=(
                                            "Could not roll back unaccepted edits after "
                                            "correction attempt mutated state then "
                                            "reported a non-FIXED verdict."
                                        ),
                                    )
                                raise AgentVerdictProtocolError(
                                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                    message=(
                                        "Correction attempt mutated the worktree then "
                                        "reported a non-FIXED verdict."
                                    ),
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
            # Capture tip only on the protocol-retry path (PRRT_kwDOSJAM6s6eJ2Tm):
            # ``verified_attempt_tip`` is only consumed when correction-start
            # rev-parse fails. Probing before parse discarded valid attempt-0
            # verdicts when Git spawn failed transiently. Still roll back on
            # ordinary tip failures here (PRRT_kwDOSJAM6s6eJUbE): attempt 0 may
            # have mutated the worktree, and OSError/RuntimeError while spawning
            # Git is outside the commit-sink Exception handlers (outer handler
            # catches only CancelledError).
            if worktree_path.exists() and callable(rev_parse_head):
                try:
                    tip_after_attempt = await rev_parse_head(worktree_path)
                except Exception as tip_exc:
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
                            "monitor.agent_verdict_post_attempt_tip_rollback_failed",
                            workspace_id=workspace_id,
                            item_start_head=item_start_head,
                            protocol_attempt=protocol_attempt,
                            exc_type=type(tip_exc).__name__,
                        )
                        raise AgentVerdictProtocolError(
                            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                            message=(
                                "Could not roll back unaccepted edits after "
                                "post-attempt tip probe failure."
                            ),
                        ) from tip_exc
                    raise
                if tip_after_attempt:
                    verified_attempt_tip = tip_after_attempt
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
        current_head_lower = current_head.lower()
        state_recorded_remote_advance = state.hosted_terminal_head_advanced or (
            current_last_push_sha.lower() != saved_last_push_sha.lower()
        )
        if state_recorded_remote_advance:
            needs_hosted_remote_rollback = True
        elif current_head_lower != start_head_lower:
            # Hosted agents publish terminal commits before AWF syncs and gates
            # them. When gating fails, ``_record_hosted_terminal_head_sync`` has not
            # run, so state still points at the pre-repair head even though the
            # local worktree and remote branch were advanced.
            needs_hosted_remote_rollback = True
        if needs_hosted_remote_rollback:
            if not state_recorded_remote_advance and current_head_lower != start_head_lower:
                candidate = current_head
            else:
                candidate = current_last_push_sha or current_head
            if candidate and candidate.lower() != start_head_lower:
                published_remote_head = candidate
            else:
                needs_hosted_remote_rollback = False

    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    merge_safety_git_env = _git_env_for_merge_safety_object_lookup()
    head_matches_start = current_head.lower() == item_start_head.lower()
    rolled_back_from: str | None = None

    async def _run_git(args: list[str]) -> CommandResult:
        return await runner._deps.runner.run(
            git_worktree_command(worktree_path, *args),
            env=merge_safety_git_env,
        )

    from awf.runtime.pr_monitor_runner.remote_repair_unpublished import (
        _live_head_matches_pinned_recovery_head,
    )
    from awf.runtime.validation_worktree import cleanup_validation_worktree_side_effects

    # Keep the live HEAD recheck, destructive reset, and cleanup in one critical
    # section. `run_worktree_git` cannot be used inside this block because it
    # acquires a separate lock per mutating command.
    async with hold_exclusive_worktree_writer_lock(worktree_path):
        head_unchanged, live_head = await _live_head_matches_pinned_recovery_head(
            runner._deps.runner,
            worktree_path=worktree_path,
            pinned_head=current_head,
            git_env=merge_safety_git_env,
        )
        if not head_unchanged:
            _log.warning(
                "monitor.agent_verdict_protocol_retry_rollback_aborted_live_worktree_changed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                current_head=current_head,
                live_head=live_head,
            )
            return False
        if not head_matches_start:
            reset = await _run_git(["reset", "--hard", item_start_head])
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

    hosted_remote_state_cleared = not needs_hosted_remote_rollback
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
        hosted_remote_state_cleared = True

    restore_local_push_tracking = hosted_remote_state_cleared
    if state is not None and restore_local_push_tracking:
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


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


async def _read_correction_pr_worthy_residue_fingerprint(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Return a fingerprint of PR-worthy dirty porcelain.

    Empty string means clean. ``None`` means the status probe failed and callers
    must fail closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.

    Path names alone are not enough: when attempt 0 leaves ``src/x.py`` dirty and
    the correction edits that same file, a path-only fingerprint collides and
    attribution treats the mutation as pre-existing residue
    (PRRT_kwDOSJAM6s6eKj9D). Include staged/unstaged diff hashes and untracked
    file content identity while retaining the runtime-path exclusion.
    """
    if not worktree_path.exists():
        return ""

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner.path_parsing import (
        _changed_paths_from_porcelain,
        _untracked_paths_from_porcelain,
    )
    from awf.runtime.validation_worktree import is_under_agent_runtime_root

    git_env = git_env_without_object_lookup_overrides()

    try:
        status = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ),
            env=git_env,
        )
    except Exception as exc:
        # Spawn failures (e.g. OSError from create_subprocess_exec) must fail
        # closed like a non-ok status so the correction mutation path rolls back
        # unaccepted dirty edits (PRRT_kwDOSJAM6s6eJi5X).
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        return None
    if not status.ok:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            returncode=status.returncode,
            stderr=(status.stderr or "")[:400],
        )
        return None
    if not (status.stdout or "").strip():
        return ""
    untracked = set(_untracked_paths_from_porcelain(status.stdout))
    paths = sorted(
        path
        for path in _changed_paths_from_porcelain(status.stdout)
        if not (path in untracked and is_under_agent_runtime_root(path))
    )
    if not paths:
        return ""

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    status_lines = sorted(
        line
        for line in (status.stdout or "").splitlines()
        if line
        and any(candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n"))
    )

    async def _diff_probe(*args: str) -> str | None:
        try:
            result = await runner._deps.runner.run(
                git_worktree_command(worktree_path, *args),
                env=git_env,
            )
        except Exception as exc:
            _log.warning(
                "monitor.agent_verdict_correction_residue_diff_failed",
                workspace_id=workspace_id,
                diff_args=list(args),
                exc_type=type(exc).__name__,
                error=str(exc)[:400],
            )
            return None
        if not result.ok:
            _log.warning(
                "monitor.agent_verdict_correction_residue_diff_failed",
                workspace_id=workspace_id,
                diff_args=list(args),
                returncode=result.returncode,
                stderr=(result.stderr or "")[:400],
            )
            return None
        return result.stdout or ""

    staged = await _diff_probe("diff", "--cached")
    if staged is None:
        return None
    unstaged = await _diff_probe("diff")
    if unstaged is None:
        return None

    untracked_hasher = hashlib.sha256()
    for path in paths:
        if path not in untracked:
            continue
        untracked_hasher.update(path.encode("utf-8", errors="surrogateescape"))
        untracked_hasher.update(b"\0")
        candidate = worktree_path / path
        try:
            # Symlinks: fingerprint link text via lstat/readlink — never follow.
            # Path.open follows targets; symlink→/dev/zero hangs the event loop
            # and large host files cause unbounded I/O (PRRT_kwDOSJAM6s6eK9AB).
            if candidate.is_symlink():
                link_text = str(candidate.readlink()).encode("utf-8", errors="surrogateescape")
                untracked_hasher.update(b"symlink:")
                untracked_hasher.update(link_text)
            else:
                with candidate.open("rb") as fh:
                    while chunk := fh.read(65536):
                        untracked_hasher.update(chunk)
        except OSError:
            untracked_hasher.update(b"<missing>")
        untracked_hasher.update(b"\0")

    return "\n".join(
        [
            *status_lines,
            f"staged:{_sha256_utf8(staged)}",
            f"unstaged:{_sha256_utf8(unstaged)}",
            f"untracked:{untracked_hasher.hexdigest()}",
        ]
    )


def _correction_authored_mutation_vs_start(
    *,
    attempt_start_head: str | None,
    pre_sink_head: str | None,
    correction_start_residue_fp: str | None,
    pre_sink_residue_fp: str | None,
) -> bool:
    """True when the correction agent mutated HEAD or dirt before the commit sink."""
    if pre_sink_head is None:
        # Cannot observe pre-sink HEAD — fail closed (PRRT_kwDOSJAM6s6eKoIe).
        return True
    if attempt_start_head is not None and pre_sink_head.lower() != attempt_start_head.lower():
        return True
    if pre_sink_residue_fp is None:
        # Cannot observe post-agent dirt — fail closed.
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: any pre-sink dirt cannot be proven pre-existing.
        return bool(pre_sink_residue_fp)
    return pre_sink_residue_fp != correction_start_residue_fp


def _stranded_residue_is_correction_mutation(
    *,
    correction_start_residue_fp: str | None,
    post_residue_fp: str | None,
) -> bool:
    """True when post-sink stranded dirt is not attributable to correction-start."""
    if post_residue_fp is None:
        return True
    if correction_start_residue_fp is None:
        return bool(post_residue_fp)
    return post_residue_fp != correction_start_residue_fp


async def _correction_attempt_left_pr_worthy_residue(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """True when uncommitted PR-worthy dirt remains after the commit sink.

    ``_commit_dirty_worktree`` may return False after status/add/commit failure
    while leaving correction edits dirty. HEAD can stay at attempt-start with
    ``dirty_changes_committed`` False, so mutation detection must probe porcelain
    before rollback accepts a non-FIXED correction verdict. Status inspection
    failure fails closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.
    """
    fingerprint = await _read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    if fingerprint is None:
        return True
    return bool(fingerprint)


async def _item_fix_evidence(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
    item_path: str | None,
    item_line: int | None,
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
    touches_path = getattr(runner, "_commit_range_touches_path", None)
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
            not callable(touches_path)
            or not await touches_path(
                worktree_path=worktree_path,
                left=item_start_head,
                right=candidate,
                path=item_path,
                line=item_line,
            )
        ):
            continue
        return True
    return False
