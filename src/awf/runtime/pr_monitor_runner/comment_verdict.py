"""Provider-neutral CLI verdict and evidence operations for PR comments."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.db.repositories import WorkspaceRepository

# ``comment_verdict_rollback`` and the extracted pre-launch block resolve these
# three through this module at call time so monkeypatches on ``comment_verdict``
# (and the ``comments`` forwarding shim) keep reaching the rollback / mirror /
# hooks-repair code after the module split.
from awf.node.git_manager import mirror_path_for_worktree as mirror_path_for_worktree
from awf.node.git_manager import repair_mirror_hooks_path as repair_mirror_hooks_path
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME as MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
)
from awf.runtime.ownership import (
    repair_agent_runtime_ownership as repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.comment_verdict_compose_cleanup import (
    sink_and_raise_compose_cleanup_error as sink_and_raise_compose_cleanup_error,
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
    path_level_item_fix_evidence as path_level_item_fix_evidence,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    preserved_correction_tip as preserved_correction_tip,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
    verdict_reason_cites_own_commit as verdict_reason_cites_own_commit,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction_mutation import (
    raise_correction_non_fixed_mutation as raise_correction_non_fixed_mutation,
)
from awf.runtime.pr_monitor_runner.comment_verdict_correction_mutation import (
    read_correction_end_head as read_correction_end_head,
)

# Re-exported (``X as X``) because ``comments`` and the tests resolve the item
# entry points through this module at call time, so a monkeypatch here still
# reaches them after the module split.
from awf.runtime.pr_monitor_runner.comment_verdict_entrypoint import (
    _invoke_cli_for_verdict as _invoke_cli_for_verdict,
)
from awf.runtime.pr_monitor_runner.comment_verdict_entrypoint import (
    _invoke_cli_for_verdict_result as _invoke_cli_for_verdict_result,
)
from awf.runtime.pr_monitor_runner.comment_verdict_prelaunch import (
    prepare_item_protocol_anchors,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT as _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    _VERDICT_PROTOCOL_CORRECTION_SUFFIX as _VERDICT_PROTOCOL_CORRECTION_SUFFIX,
)

# The protocol vocabulary and result types live in a sibling module; every name
# is re-exported (``X as X``) because this module stays their import surface.
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_FIXED_WITHOUT_EVIDENCE as AGENT_FIXED_WITHOUT_EVIDENCE,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_NON_FIXED_WITH_MUTATION as AGENT_NON_FIXED_WITH_MUTATION,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_VERDICT_PROTOCOL_VIOLATION as AGENT_VERDICT_PROTOCOL_VIOLATION,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AgentVerdict as AgentVerdict,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AgentVerdictExecutionError as AgentVerdictExecutionError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AgentVerdictProtocolError as AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    MonitorVerdict as MonitorVerdict,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    MonitorVerdictResult as MonitorVerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    Verdict as Verdict,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    VerdictResult as VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
    _correction_authored_mutation_vs_start,
    _fingerprint_has_pr_worthy_path_residue,
    _stranded_residue_is_correction_mutation,
)

# Re-exported (``X as X``) because the extracted timeout-preserve block resolves
# the residue probe through this module at call time, so a monkeypatch here still
# reaches it.
from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
    _read_correction_pr_worthy_residue_fingerprint as _read_correction_pr_worthy_residue_fingerprint,
)

# Re-exported (``X as X``) because the extracted pre-launch block resolves the
# git-config snapshot through this module at call time.
from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
    remember_item_start_local_git_configs as remember_item_start_local_git_configs,
)

# Re-exported (``X as X``) because the extracted correction-end probe resolves it
# through this module at call time, so a monkeypatch here still reaches it.
from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
    read_protocol_attempt_start_head as read_protocol_attempt_start_head,
)

# ``_item_fix_evidence`` is re-exported (``X as X``) because the correction
# path and other call sites resolve it through this module at call time, so a
# monkeypatch on ``comment_verdict`` still reaches the line-anchored evidence
# check.
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _item_fix_evidence as _item_fix_evidence,
)

# Re-exported (``X as X``) for the same reason: the extracted timeout-preserve
# block resolves the hooks repair through this module at call time.
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _repair_mirror_hooks_or_raise as _repair_mirror_hooks_or_raise,
)

# Explicitly re-exported: the extracted sibling blocks resolve both rollbacks
# through this module at call time, so a monkeypatch here still reaches them.
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _rollback_or_classify_failure as _rollback_or_classify_failure,
)
from awf.runtime.pr_monitor_runner.comment_verdict_rollback import (
    _rollback_unaccepted_protocol_retry_changes as _rollback_unaccepted_protocol_retry_changes,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    TimeoutSinkOutcome as TimeoutSinkOutcome,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    _sink_timeout_dirty_changes as _sink_timeout_dirty_changes,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    cleanup_error_agent_timeout_reason_code as cleanup_error_agent_timeout_reason_code,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    consume_item_start_head as consume_item_start_head,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    handle_agent_run_error as handle_agent_run_error,
)
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    preserve_timeout_work_and_raise_cleanup_error as preserve_timeout_work_and_raise_cleanup_error,
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


async def _run_item_verdict_protocol(
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
    timeout_rerun_anchor_sink: list[str] | None = None,
) -> VerdictResult:
    """Run one logical item with at most one protocol-correction attempt.

    Provider execution/recovery errors are outside the protocol retry budget.
    Both protocol attempts share the item-start HEAD. FIXED evidence is
    recomputed from the final candidate HEAD after each attempt, not OR-
    accumulated across attempts, so a correction retry that reverts an
    unaccepted first-attempt commit cannot inherit stale evidence. Attempt 0 is
    strictly line-anchored, so a misplaced or absent FIXED still earns its
    correction round. On the correction attempt — whatever rejected attempt 0 —
    evidence is re-checked at *path* level over the item's own
    ``item_start_head``..HEAD range: the agent has been told its FIXED lacked
    line evidence and re-affirmed it, and the range cannot hold a stale or
    foreign commit, so a contentful change to the reviewed file is an honest
    off-anchor fix and is accepted as ``fix_committed`` (#925 D1). A FIXED whose
    commit touches none of the reviewed paths is preserved and escalated to
    ``needs_human`` instead of terminating the protocol, and a corrected
    ``FALSE POSITIVE`` / ``DEFER`` / ``NEEDS_HUMAN`` whose reason cites this
    item's own attempt-0 commit is never accepted as a non-fix — the commit is
    kept. ``FALSE POSITIVE`` / ``DEFER`` return ``fix_committed`` when
    item-scoped evidence exists, otherwise ``needs_human``; an explicit
    corrected ``NEEDS_HUMAN`` always stays ``needs_human`` so evidence cannot
    override a requested human gate (#925, issue:5558086911). A FIXED with no
    contentful change at all still terminates after its one correction. A corrected
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
    edits back first. Rollback never rewinds past this attempt's own start: on a
    re-attempt after a preserved timeout the evidence anchor is restored to the
    original item start, but the rollback floor stays at the preserved HEAD so no
    later bad verdict can delete the commits #932 deliberately kept (#934).
    ``evidence_item_id`` keys the #932 timeout marker and ``evidence_body_hash``
    binds it to the feedback body it was written for; no evidence is persisted or
    salvaged across process restarts. ``timeout_rerun_anchor_sink`` reports the
    anchor a timeout the service-recovery loop reran over left owing, so the
    caller can re-arm it if this item ends without a verdict
    (PRRT_kwDOSJAM6s6fwTyP).
    """
    # Retained (no longer fully ``del``-ed) purely as the key and body binding for
    # the #932 timeout marker below; no evidence is persisted or salvaged from them.
    timeout_preserve_item_id = (evidence_item_id or "").strip() or None
    timeout_preserve_body_hash = (evidence_body_hash or "").strip() or None
    del evidence_item_id, evidence_body_hash
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    anchors = await prepare_item_protocol_anchors(
        runner,
        workspace_id=workspace_id,
        state=state,
        timeout_preserve_item_id=timeout_preserve_item_id,
        operation_start_head=operation_start_head,
        evidence_item_path=evidence_item_path,
        evidence_item_line=evidence_item_line,
        evidence_anchor_head=evidence_anchor_head,
    )
    worktree_path = anchors.worktree_path
    item_path = anchors.item_path
    item_line = anchors.item_line
    item_start_head = anchors.item_start_head
    rollback_floor_head = anchors.rollback_floor_head
    mirror_path = anchors.mirror_path
    rev_parse_head = anchors.rev_parse_head
    command_evidence: list[str] = []

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
    # correction attempt refuses to roll back a self-citing non-fix and re-checks
    # evidence at path level (#925), whatever rejected attempt 0.
    fixed_without_evidence_correction = False
    # Set once the rollback floor has been raised past a timeout the service-
    # recovery loop reran over (PRRT_kwDOSJAM6s6fvdil). It holds the ordinary
    # floor as it stood before that raise — never as a rollback target
    # (PRRT_kwDOSJAM6s6fvw8m), only as the "did this item leave work behind?"
    # baseline a later timeout is measured against.
    pre_timeout_rerun_floor_head: str | None = None
    timeout_rerun_floor_raised = False
    # Non-empty once the #932 preserve handler has entered its timeout sequence.
    # That sequence keeps the timed-out agent's work through several awaits, and
    # ``CancelledError`` bypasses its handlers, so the cancellation branch below
    # must read this as "the floor is protected — do not rewind"
    # (PRRT_kwDOSJAM6s6fylWD).
    timeout_preservation_protected: list[str] = []

    async def sink_timeout_rerun_dirty_changes(reason_code: str) -> bool:
        """Commit a reran-over timed-out run's uncommitted edits (#932).

        A published rerun floor is only a SHA, so it cannot hold edits the
        timed-out run never committed: with no commits of its own that floor
        equals the attempt's own floor, and the rollback a provider or protocol
        failure on the rerun performs resets straight through them. Running the
        preserve handler's own dirty sink first turns them into a commit the
        published floor does cover (PRRT_kwDOSJAM6s6fvw8r). Reads
        ``item_start_head`` live: it is the sink anchor as of the run this
        salvage belongs to.

        Returns whether the recovery loop may rerun over this run. The sink's own
        False is not that answer: it also means "nothing to commit" (clean
        worktree, or the sink disabled), which is the ordinary case and must not
        cost the rerun. Only stranded PR-worthy dirt does — the salvage failed,
        the published SHA floor cannot cover those edits, and the rollback a
        provider failure or non-FIXED verdict on the rerun performs would delete
        work #932 promised to keep. Probe for it and fail closed on an unreadable
        probe: preserving the timeout costs one rerun, guessing costs the edits
        (PRRT_kwDOSJAM6s6fwTyO).
        """
        sink_outcome = await _sink_timeout_dirty_changes(
            runner,
            workspace_id=workspace_id,
            reason_code=reason_code,
            item_start_head=item_start_head,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            command_evidence=command_evidence,
            commit_dirty_changes=commit_dirty_changes,
        )
        if sink_outcome is TimeoutSinkOutcome.COMMITTED:
            return True
        try:
            residue_fp = await _read_correction_pr_worthy_residue_fingerprint(
                runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
            )
        except Exception as probe_exc:
            # Broad on purpose, like every other residue probe here: it spawns
            # Git and can raise outside the git-spawn error set. Fail closed —
            # an unreadable worktree cannot prove the edits were salvaged.
            # ``asyncio.CancelledError`` is a ``BaseException`` and still
            # propagates.
            _log.warning(
                "monitor.agent_verdict_timeout_rerun_residue_probe_failed",
                workspace_id=workspace_id,
                reason_code=reason_code,
                exc_type=type(probe_exc).__name__,
            )
            return False
        if residue_fp is None:
            return False
        return not _fingerprint_has_pr_worthy_path_residue(residue_fp)

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
                    item_start_head=rollback_floor_head,
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
                        if protocol_attempt == 0 and rollback_floor_head is None:
                            rollback_floor_head = parsed_attempt_start
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
                # The service-recovery loop intercepts a watchdog timeout that
                # coincides with an unhealthy agent service, restarts the service
                # and reruns the agent — so that run's commits never reach the
                # #932 preserve handler below. It publishes the HEAD it reran
                # over here (PRRT_kwDOSJAM6s6fvdil).
                timeout_rerun_floor_heads: list[str] = []
                # Baseline for "did this item leave work behind?" — the floor as
                # it stood before *any* rerun raise, not just this attempt's. An
                # earlier attempt's raise carries the timed-out run's commits, so
                # measuring a later timeout against it would hide exactly the
                # work the raise protects and the operator-hint retry gate would
                # read "nothing survived".
                attempt_floor_before_rerun = (
                    pre_timeout_rerun_floor_head
                    if timeout_rerun_floor_raised
                    else rollback_floor_head
                )
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
                        timeout_rerun_floor_sink=timeout_rerun_floor_heads,
                        timeout_rerun_dirty_sink=sink_timeout_rerun_dirty_changes,
                    )
                finally:
                    # Raise the floor on *every* exit from the run, raising or
                    # returning, and for every later exit of this item —
                    # including acceptance of the rerun's verdict
                    # (PRRT_kwDOSJAM6s6fvw8m). Whatever ends the item rolls back
                    # through the very same handlers a provider failure does,
                    # and none of them may rewind past work the timed-out run
                    # left behind: a non-FIXED verdict from the rerun would
                    # otherwise delete commits #932 promised to keep, which the
                    # cross-pass re-attempt above already refuses to do (its
                    # floor stays at the preserved HEAD). The rerun's own
                    # unaccepted residue still rolls back — to this floor.
                    if timeout_rerun_floor_heads:
                        if not timeout_rerun_floor_raised:
                            pre_timeout_rerun_floor_head = rollback_floor_head
                            timeout_rerun_floor_raised = True
                            # The floor keeps the timed-out run's commit; the
                            # item-start marker is what keeps it inside the next
                            # attempt's FIXED evidence range. The preserve handler
                            # that writes that marker never saw this timeout — the
                            # recovery loop intercepted it — so publish the anchor
                            # it owes. The caller re-arms it only if this item ends
                            # without a verdict, which keeps consume-on-verdict
                            # intact (PRRT_kwDOSJAM6s6fwTyP).
                            if timeout_rerun_anchor_sink is not None and item_start_head:
                                timeout_rerun_anchor_sink.append(item_start_head)
                        rollback_floor_head = timeout_rerun_floor_heads[-1]
            except AgentRunError as exc:
                append_command_evidence(
                    command_evidence,
                    stdout=exc.result.stdout,
                    stderr=exc.result.stderr,
                )
                # A provider failure still rolls unaccepted edits back; a
                # watchdog timeout preserves the item's work instead (#932).
                await handle_agent_run_error(
                    runner,
                    exc=exc,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    rollback_floor_head=rollback_floor_head,
                    timeout_work_baseline_head=attempt_floor_before_rerun,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                    item_id=timeout_preserve_item_id,
                    item_body_hash=timeout_preserve_body_hash,
                    commit_message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    task_tag=task_tag,
                    command_evidence=command_evidence,
                    commit_dirty_changes=commit_dirty_changes,
                    rev_parse_head=rev_parse_head,
                    timeout_preservation_sink=timeout_preservation_protected,
                )
            except ProviderRecoveryRetryError as exc:
                # ``_run_monitor_agent_with_service_recovery`` can raise this from its
                # post-restart pre-launch guard after the agent already edited or
                # self-committed. Roll back before propagating so unaccepted residue
                # does not wedge the dirty-worktree gate on the next pass.
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=rollback_floor_head,
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
                    item_start_head=rollback_floor_head,
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
                timeout_reason_code = cleanup_error_agent_timeout_reason_code(exc)
                if timeout_reason_code is not None:
                    # The adapter runs compose cleanup *before* raising its
                    # ``AgentRunError``, so a watchdog timeout whose cleanup also
                    # failed arrives here and would be rolled back — deleting the
                    # timed-out agent's commits. Preserve the work as #932 does
                    # and still escalate the cleanup error (PRRT_kwDOSJAM6s6fvPT_).
                    await preserve_timeout_work_and_raise_cleanup_error(
                        runner,
                        exc=exc,
                        timeout_reason_code=timeout_reason_code,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        item_start_head=item_start_head,
                        state=state,
                        item_id=timeout_preserve_item_id,
                        item_body_hash=timeout_preserve_body_hash,
                        commit_message=commit_message,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        task_tag=task_tag,
                        command_evidence=command_evidence,
                        commit_dirty_changes=commit_dirty_changes,
                        mirror_path=mirror_path,
                    )
                # Agent output may exist even when compose cleanup fails. Roll back
                # before mirror repair, then attempt the dirty-worktree sink before
                # re-raising so uncommitted residue cannot block remonitor.
                rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=rollback_floor_head,
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
                            item_start_head=rollback_floor_head,
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
                    item_start_head=rollback_floor_head,
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
                # Sinks the dirty worktree, rolls back to the attempt floor, then
                # re-raises: cleanup failure is the outcome, never a verdict.
                await sink_and_raise_compose_cleanup_error(
                    runner,
                    compose_cleanup_error=compose_cleanup_error,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    rollback_floor_head=rollback_floor_head,
                    item_start_last_push_sha=item_start_last_push_sha,
                    state=state,
                    protocol_attempt=protocol_attempt,
                    commit_message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    task_tag=task_tag,
                    command_evidence=command_evidence,
                    commit_dirty_changes=commit_dirty_changes,
                )

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
                if (
                    not logical_fix_evidence
                    and protocol_attempt == 1
                    and item_path is not None
                    and item_line is not None
                    and item_line > 0
                ):
                    # Path-level evidence on the correction (#925 D1, restored on
                    # top of #928). Attempt 0 already failed the strict
                    # line-anchored gate and the agent was told so; a re-affirmed
                    # FIXED whose contentful commit changes the anchored *path*
                    # is an honest off-anchor fix (helper above the caller, guard
                    # at the call site), not an unsupported claim, and escalating
                    # it to a human is the unnecessary escalation. Safe because
                    # this is never the first gate and the probe runs over the
                    # item's own ``item_start_head``..HEAD range, so the commit
                    # cannot be stale or foreign. ``item_line <= 0`` is the
                    # unmappable-anchor sentinel from the path/line remap above:
                    # those stay fail-closed on both attempts
                    # (PRRT_kwDOSJAM6s6dFLGV); ``item_line is None`` means the
                    # check above was already path-level. Inside the commit-sink
                    # ``try`` so it shares the rollback / reason-code handlers.
                    logical_fix_evidence = await path_level_item_fix_evidence(
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
                    item_start_head=rollback_floor_head,
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
                    item_start_head=rollback_floor_head,
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
                                item_start_head=rollback_floor_head,
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
                                    item_start_head=rollback_floor_head,
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
                            # Fails closed on both an unreadable probe and a
                            # transient ``None``, rolling back first so the
                            # correction attempt's edits are never stranded.
                            post_attempt_head = await read_correction_end_head(
                                runner,
                                workspace_id=workspace_id,
                                worktree_path=worktree_path,
                                rev_parse_head=rev_parse_head,
                                attempt_start_head=attempt_start_head,
                                item_start_head=item_start_head,
                                rollback_floor_head=rollback_floor_head,
                                item_start_last_push_sha=item_start_last_push_sha,
                                state=state,
                                protocol_attempt=protocol_attempt,
                                verdict=parsed.verdict,
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
                                # Rolls back to the floor, then refuses the
                                # verdict as mutation or — when the pre-sink
                                # probe failed — as an unmeasurable attempt.
                                await raise_correction_non_fixed_mutation(
                                    runner,
                                    workspace_id=workspace_id,
                                    worktree_path=worktree_path,
                                    rollback_floor_head=rollback_floor_head,
                                    item_start_last_push_sha=item_start_last_push_sha,
                                    state=state,
                                    protocol_attempt=protocol_attempt,
                                    attempt_start_head=attempt_start_head,
                                    post_attempt_head=post_attempt_head,
                                    verdict=parsed.verdict,
                                    dirty_changes_committed=dirty_changes_committed,
                                    stranded_dirty_residue=stranded_dirty_residue,
                                    pre_sink_head_unreadable=pre_sink_head_unreadable,
                                    pre_sink_probe_exc=pre_sink_probe_exc,
                                )
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
                                # DEFER become FIXED when item-scoped evidence
                                # exists — line-anchored, or the correction-time
                                # path-level re-check above; an explicit
                                # NEEDS_HUMAN stays escalated.
                                return correction_self_citation_outcome(
                                    workspace_id=workspace_id,
                                    verdict=parsed.verdict,
                                    reason=parsed.reason,
                                    attempt_tip=self_citation_tip,
                                    has_path_evidence=logical_fix_evidence,
                                )
                        # Only the rerun's own residue goes away: the floor still
                        # holds at the HEAD the service-recovery loop reran over,
                        # so a parsed non-FIXED verdict cannot delete the commits
                        # the timed-out run left behind (PRRT_kwDOSJAM6s6fvw8m).
                        rollback_ok = await _rollback_or_classify_failure(
                            runner,
                            workspace_id=workspace_id,
                            worktree_path=worktree_path,
                            item_start_head=rollback_floor_head,
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
                    item_start_head=rollback_floor_head,
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
                        item_start_head=rollback_floor_head,
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
            # unaccepted residue cannot be pushed on a later repair cycle — unless
            # the #932 preserve handler already claimed this attempt's work as a
            # timeout's. Its sink / residue / HEAD / provider-recovery awaits are
            # all cancellable, and rewinding to the attempt floor here would
            # delete the timed-out agent's commits and any salvaged sink commit,
            # which is the destruction #932 exists to prevent
            # (PRRT_kwDOSJAM6s6fylWD). The edits stay exactly as the uncancelled
            # preserve path leaves them, marker included.
            if timeout_preservation_protected:
                _log.warning(
                    "monitor.agent_verdict_cancellation_preserved_timeout_work",
                    workspace_id=workspace_id,
                    item_start_head=item_start_head,
                    protocol_attempt=protocol_attempt,
                    reason_code=timeout_preservation_protected[-1],
                )
                raise
            rollback_ok = await _rollback_unaccepted_protocol_retry_changes(
                runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                item_start_head=rollback_floor_head,
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
