"""Provider-neutral CLI verdict and evidence operations for PR comments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    mirror_path_for_worktree,
)

# ``comment_verdict_rollback`` resolves these two through this module at call time
# so monkeypatches on ``comment_verdict`` (and the ``comments`` forwarding shim)
# keep reaching the rollback / hooks-repair code after the module split.
from awf.node.git_manager import repair_mirror_hooks_path as repair_mirror_hooks_path
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    AGENT_NON_FIX_CITES_OWN_COMMIT as AGENT_NON_FIX_CITES_OWN_COMMIT,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    correction_reason_cites_own_item_commit as correction_reason_cites_own_item_commit,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    correction_self_citation_outcome as correction_self_citation_outcome,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    correction_unscoped_fix_outcome as correction_unscoped_fix_outcome,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    preserved_correction_tip as preserved_correction_tip,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    verdict_reason_cites_own_commit as verdict_reason_cites_own_commit,
)
from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
    _correction_authored_mutation_vs_start,
    _fingerprint_has_pr_worthy_path_residue,
    _read_correction_pr_worthy_residue_fingerprint,
    _stranded_residue_is_correction_mutation,
    remember_item_start_local_git_configs,
)
from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    read_protocol_attempt_start_head,
)

# ``_item_fix_evidence`` is re-exported (``X as X``) because the correction
# path and other call sites resolve it through this module at call time, so a
# monkeypatch on ``comment_verdict`` still reaches the line-anchored evidence
# check.
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _item_fix_evidence as _item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _repair_mirror_hooks_or_raise,
    _rollback_or_classify_failure,
    _rollback_unaccepted_protocol_retry_changes,
)
from awf.runtime.pr_monitor_runner.constants import (
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
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
from awf.runtime.worktree_writer_lock import (
    hold_exclusive_worktree_writer_lock as hold_exclusive_worktree_writer_lock,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner


_log = get_logger(__name__)

AGENT_VERDICT_PROTOCOL_VIOLATION = "AGENT_VERDICT_PROTOCOL_VIOLATION"
AGENT_FIXED_WITHOUT_EVIDENCE = "AGENT_FIXED_WITHOUT_EVIDENCE"
AGENT_NON_FIXED_WITH_MUTATION = "AGENT_NON_FIXED_WITH_MUTATION"

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
    # True when this verdict deliberately keeps an unpushed local commit the
    # agent authored for the item (the #925 correction outcomes). Such a


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
    "duplicate of a different review item, or was already addressed by a commit "
    "made before this item started, choose FALSE POSITIVE and state that reason. "
    "A commit you already made for this review item does not count as such an "
    "earlier commit: do not cite it as the reason for FALSE POSITIVE or DEFER — "
    "repeat FIXED and describe that change instead (#925)."
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
    unaccepted first-attempt commit cannot inherit stale evidence. Related
    off-anchor fixes (near-anchor inserts, call-site→definition changes) are
    accepted by the line-scoped evidence gate; path membership alone is never
    enough for ``fix_committed`` (issue:5558086911). On the correction attempt —
    whatever rejected attempt 0 — a FIXED whose contentful commit carries no
    item-scoped evidence is preserved and escalated to ``needs_human`` instead
    of terminating the protocol, and a corrected ``FALSE POSITIVE`` / ``DEFER``
    / ``NEEDS_HUMAN`` whose reason cites this item's own attempt-0 commit is
    never accepted as a non-fix — the commit is kept. ``FALSE POSITIVE`` /
    ``DEFER`` return ``fix_committed`` when related-line evidence already
    exists, otherwise ``needs_human``; an explicit corrected ``NEEDS_HUMAN``
    always stays ``needs_human`` so evidence cannot override a requested human
    gate (#925, issue:5558086911). A FIXED with no contentful change at all
    still terminates after its one correction. A corrected
    non-FIXED verdict is accepted only when the correction attempt itself did
    not advance HEAD, commit dirty changes it authored, leave new PR-worthy
    uncommitted residue after a False commit sink, or otherwise mutate relative
    to the HEAD and dirty state at the start of that attempt. Pre-existing
    attempt-0 residue left by a False first sink is not attributed to a clean
    correction: sinking or re-detecting that same residue still rolls back to
    item-start and accepts the verdict (PRRT_kwDOSJAM6s6eKNQT). Mutation plus
    non-FIXED is ``AGENT_NON_FIXED_WITH_MUTATION`` after safe rollback. First-attempt
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

    # Snapshot after hooksPath repair so non-FIXED rollback cannot reintroduce a
    # poisoned executable hook path the pre-launch safety repair just removed
    # (PRRT_kwDOSJAM6s6e0yQN). Off the event loop: nested-.git discovery walks
    # the full worktree under a 100k-entry / 30s budget (PRRT_kwDOSJAM6s6e5nws).
    if worktree_path.exists() and not await asyncio.to_thread(
        remember_item_start_local_git_configs,
        worktree_path,
    ):
        # Fingerprint probes fail closed when local config cannot be snapshotted.
        # Do not abort the item here: unit fixtures often use non-contained
        # ``gitdir:`` stubs, and production still refuses config-blind non-FIXED
        # acceptance via ``None`` residue fingerprints (PRRT_kwDOSJAM6s6e0Xdl).
        _log.warning(
            "monitor.agent_verdict_item_start_git_config_snapshot_failed",
            workspace_id=workspace_id,
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
    # True once attempt 0 has been rejected specifically for missing line-anchored
    # FIXED evidence. It selects the extra correction prompt context only: every
    # correction attempt refuses to roll back a self-citing non-fix (#925), and
    # none of them widen FIXED evidence to path membership alone.
    fixed_without_evidence_correction = False

    for protocol_attempt in range(2):
        dirty_changes_committed = False
        compose_cleanup_error: ComposeExecCleanupError | None = None
        attempt_start_head = item_start_head
        correction_authored_mutation = False
        pre_sink_head_unreadable = False
        pre_sink_probe_exc: Exception | None = None
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
                # Prefer remembered item-start configs + timeout so a live
                # include.path → FIFO cannot hang the worker (PRRT_kwDOSJAM6s6e30Rp).
                if worktree_path.exists():
                    parsed_attempt_start = await read_protocol_attempt_start_head(
                        runner,
                        worktree_path=worktree_path,
                        rev_parse_head=rev_parse_head if callable(rev_parse_head) else None,
                    )
                    if parsed_attempt_start:
                        attempt_start_head = parsed_attempt_start
                        if protocol_attempt == 0 and item_start_head is None:
                            # Pre-loop HEAD read can fail transiently while this
                            # probe succeeds. Persist the recovered baseline so
                            # rollback anchors remain available for non-FIXED
                            # acceptance (PRRT_kwDOSJAM6s6eQPqe).
                            item_start_head = parsed_attempt_start
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
                rollback_ok = await _rollback_or_classify_failure(
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
                    if worktree_path.exists():
                        # Prefer remembered item-start configs + timeout so a
                        # live include.path → FIFO cannot hang the worker
                        # (PRRT_kwDOSJAM6s6e4egQ). Same helper as attempt-start.
                        try:
                            live_pre_sink = await read_protocol_attempt_start_head(
                                runner,
                                worktree_path=worktree_path,
                                rev_parse_head=(
                                    rev_parse_head if callable(rev_parse_head) else None
                                ),
                            )
                        except (TimeoutError, OSError, RuntimeError) as pre_sink_exc:
                            # Match other HEAD probes: log redacted cause + exc_type
                            # and fail closed without absorbing worker CancelledError
                            # (reviews 5096023656, 5098769688).
                            pre_sink_probe_exc = pre_sink_exc
                            _log.warning(
                                "monitor.agent_verdict_correction_pre_sink_head_probe_failed",
                                workspace_id=workspace_id,
                                protocol_attempt=protocol_attempt,
                                exc_type=type(pre_sink_exc).__name__,
                                error=redact_secrets(str(pre_sink_exc))[:400],
                            )
                            live_pre_sink = None
                        pre_sink_head = live_pre_sink if live_pre_sink else None
                        if pre_sink_head is None:
                            pre_sink_head_unreadable = True
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
                    unscoped_fix_evidence = False
                    if protocol_attempt == 1:
                        # Anchor-free probe: "did this item commit anything at
                        # all?", never "is that commit the fix". It only decides
                        # between preserving the commit and terminating the
                        # protocol; path membership still cannot buy
                        # ``fix_committed`` (issue:5558086911).
                        #
                        # It re-runs Git ancestry/tree checks after the
                        # commit-sink evidence handler above has ended, so an
                        # ordinary rev-parse/ancestry/repository failure would
                        # escape without rollback or reason-code classification
                        # and strand the unaccepted correction commit in the
                        # worktree (PRRT_kwDOSJAM6s6fpjBu). Guard it like the
                        # correction-end HEAD probe: roll back, then re-raise so
                        # reason-coded causes reach fix_cycle unmasked.
                        try:
                            unscoped_fix_evidence = await _item_fix_evidence(
                                runner,
                                worktree_path=worktree_path,
                                item_start_head=item_start_head,
                                item_path=None,
                                item_line=None,
                                state=state,
                                dirty_changes_committed=dirty_changes_committed,
                            )
                        except Exception as unscoped_exc:
                            rollback_ok = await _rollback_or_classify_failure(
                                runner,
                                workspace_id=workspace_id,
                                worktree_path=worktree_path,
                                item_start_head=item_start_head,
                                item_start_last_push_sha=item_start_last_push_sha,
                                state=state,
                            )
                            if not rollback_ok:
                                _log.warning(
                                    "monitor.agent_verdict_unscoped_evidence_rollback_failed",
                                    workspace_id=workspace_id,
                                    item_start_head=item_start_head,
                                    protocol_attempt=protocol_attempt,
                                    exc_type=type(unscoped_exc).__name__,
                                )
                                raise AgentVerdictProtocolError(
                                    reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                                    message=(
                                        "Could not roll back unaccepted edits after "
                                        "unscoped fix-evidence probe failure."
                                    ),
                                ) from unscoped_exc
                            raise
                    if unscoped_fix_evidence:
                        # A contentful commit exists but carries no item-scoped
                        # evidence (wrong file, or the reviewed file away from
                        # the anchored line). Rolling it back and failing the
                        # whole monitor is the #925 defect in another coat, and
                        # the shape that killed ws_46bc0f45 on PR #922 after a
                        # protocol-violation correction: keep the commit and
                        # escalate the item instead. Cite the commit that is
                        # actually preserved: ``attempt_start_head`` /
                        # ``verified_attempt_tip`` are both pre-correction, so
                        # a correction-authored commit would be reported under
                        # the original SHA (PRRT_kwDOSJAM6s6fpjBy).
                        preserved_tip = await preserved_correction_tip(
                            runner,
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                            rev_parse_head=rev_parse_head,
                            fallback=attempt_start_head or verified_attempt_tip,
                        )
                        return correction_unscoped_fix_outcome(
                            workspace_id=workspace_id,
                            reason=parsed.reason,
                            attempt_tip=preserved_tip,
                            item_path=item_path,
                        )
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
                                rollback_ok = await _rollback_or_classify_failure(
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
                            if worktree_path.exists():
                                # Correction-end probe must roll back on ordinary
                                # failures (PRRT_kwDOSJAM6s6eJ2Tg): after the
                                # correction attempt may have mutated the
                                # worktree, OSError/RuntimeError while spawning
                                # Git is outside Exception handlers here, and
                                # the surrounding handler catches only
                                # CancelledError. Match the post-attempt tip
                                # probe (PRRT_kwDOSJAM6s6eJUbE). Prefer trusted
                                # item-start configs + timeout so include.path
                                # → FIFO cannot hang (PRRT_kwDOSJAM6s6e4egQ).
                                try:
                                    live_head = await read_protocol_attempt_start_head(
                                        runner,
                                        worktree_path=worktree_path,
                                        rev_parse_head=(
                                            rev_parse_head if callable(rev_parse_head) else None
                                        ),
                                    )
                                except Exception as end_head_exc:
                                    rollback_ok = await _rollback_or_classify_failure(
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
                                elif correction_start_residue_fp is None or (
                                    not _fingerprint_has_pr_worthy_path_residue(
                                        correction_start_residue_fp
                                    )
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
                                if pre_sink_head_unreadable:
                                    mutation_reason_code = AGENT_VERDICT_PROTOCOL_VIOLATION
                                    mutation_log_event = (
                                        "monitor.agent_verdict_correction_pre_sink_head_unreadable"
                                    )
                                    mutation_message = (
                                        "Pre-sink HEAD was unreadable; cannot accept a "
                                        "non-FIXED verdict without measuring whether the "
                                        "correction attempt self-committed."
                                    )
                                    rollback_failure_message = (
                                        "Could not roll back unaccepted edits after "
                                        "correction attempt with unreadable pre-sink HEAD."
                                    )
                                else:
                                    mutation_reason_code = AGENT_NON_FIXED_WITH_MUTATION
                                    mutation_log_event = (
                                        "monitor.agent_verdict_correction_non_fixed_with_mutation"
                                    )
                                    mutation_message = (
                                        "Correction attempt mutated the worktree then "
                                        "reported a non-FIXED verdict."
                                    )
                                    rollback_failure_message = (
                                        "Could not roll back unaccepted edits after "
                                        "correction attempt mutated state then "
                                        "reported a non-FIXED verdict."
                                    )
                                _log.warning(
                                    mutation_log_event,
                                    workspace_id=workspace_id,
                                    reason_code=mutation_reason_code,
                                    protocol_attempt=protocol_attempt,
                                    attempt_start_head=attempt_start_head,
                                    current_head=post_attempt_head,
                                    verdict=parsed.verdict,
                                    dirty_changes_committed=dirty_changes_committed,
                                    stranded_dirty_residue=stranded_dirty_residue,
                                )
                                rollback_ok = await _rollback_or_classify_failure(
                                    runner,
                                    workspace_id=workspace_id,
                                    worktree_path=worktree_path,
                                    item_start_head=item_start_head,
                                    item_start_last_push_sha=item_start_last_push_sha,
                                    state=state,
                                )
                                if not rollback_ok:
                                    rollback_error = AgentVerdictProtocolError(
                                        reason_code=mutation_reason_code,
                                        message=rollback_failure_message,
                                    )
                                    if pre_sink_probe_exc is not None:
                                        raise rollback_error from pre_sink_probe_exc
                                    raise rollback_error
                                mutation_error = AgentVerdictProtocolError(
                                    reason_code=mutation_reason_code,
                                    message=mutation_message,
                                )
                                if pre_sink_probe_exc is not None:
                                    raise mutation_error from pre_sink_probe_exc
                                raise mutation_error
                            # ``verified_attempt_tip`` stays unset when the
                            # post-attempt tip probe returns None, even though the
                            # correction-start probe can recover the same attempt-0
                            # commit into ``attempt_start_head``
                            # (PRRT_kwDOSJAM6s6fmmha). Prefer that verified
                            # correction-start HEAD when it advanced past
                            # ``item_start_head``: everything in that range belongs
                            # to attempt 0 of this item, so citing it is
                            # self-citation. An equal (or unknown-baseline) head is
                            # left to ``verified_attempt_tip`` so citing a
                            # genuinely earlier commit stays non-self-citing.
                            self_citation_tip = verified_attempt_tip
                            if (
                                attempt_start_head is not None
                                and item_start_head is not None
                                and attempt_start_head.lower() != item_start_head.lower()
                            ):
                                self_citation_tip = attempt_start_head
                            if (
                                protocol_attempt == 1
                                and parsed.verdict in ("false_positive", "defer", "needs_human")
                                and await correction_reason_cites_own_item_commit(
                                    runner,
                                    reason=parsed.reason,
                                    worktree_path=worktree_path,
                                    item_start_head=item_start_head,
                                    attempt_tip=self_citation_tip,
                                )
                            ):
                                # #925 D2: the correction prompt puts this item's
                                # own attempt-0 commit at HEAD, so the agent can
                                # answer "already addressed by <that sha>". Never
                                # roll a fix back on the strength of a verdict
                                # that cites it — keep the commit. FALSE POSITIVE /
                                # DEFER become FIXED when related-line evidence
                                # exists; an explicit NEEDS_HUMAN stays escalated.
                                return correction_self_citation_outcome(
                                    workspace_id=workspace_id,
                                    verdict=parsed.verdict,
                                    reason=parsed.reason,
                                    attempt_tip=self_citation_tip,
                                    has_path_evidence=logical_fix_evidence,
                                )
                        rollback_ok = await _rollback_or_classify_failure(
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
                rollback_ok = await _rollback_or_classify_failure(
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
            # catches only CancelledError). Prefer trusted item-start configs +
            # timeout so include.path → FIFO cannot hang (PRRT_kwDOSJAM6s6e4egQ);
            # same helper as pre-sink and correction-end post-agent probes.
            if worktree_path.exists():
                try:
                    tip_after_attempt = await read_protocol_attempt_start_head(
                        runner,
                        worktree_path=worktree_path,
                        rev_parse_head=(rev_parse_head if callable(rev_parse_head) else None),
                    )
                except Exception as tip_exc:
                    rollback_ok = await _rollback_or_classify_failure(
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
            fixed_without_evidence_correction = (
                protocol_error.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
            )
            correction_context = (
                f"\n\n{_FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT}"
                if fixed_without_evidence_correction
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
