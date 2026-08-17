"""CLI verdict and owned-path operations for PR comment handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.monitor_state_keys import (
    _salvaged_fix_body_hash_state_key,
    _salvaged_fix_head_state_key,
    _salvaged_fix_start_state_key,
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
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.types import (
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

# Verdicts the CLI reply parser can produce. Kept as a type alias so callers
# (and tests) can match against a closed set.
Verdict = Literal["fix_committed", "false_positive", "defer", "needs_human", "agent_failed"]

# Fail-closed parser reasons. When the CLI also exited non-zero, prefer
# ``agent_failed`` so the monitor retries instead of parking a human escalation
# on a crash that produced no explicit NEEDS_HUMAN claim.
_FAIL_CLOSED_VERDICT_REASONS = frozenset(
    {
        "empty_verdict_output",
        "unrecognized_or_markerless_verdict",
        "garbled_verdict_marker",
        "fixed_placeholder_echo",
        "verdict_placeholder_echo",
    }
)

# Synthetic needs_human reasons from fail-closed parse or FIXED evidence gating.
# A NEEDS_HUMAN clarification re-ask must not treat these as a successful
# human-decision reason that overwrites a reasonless blocker.
_SYNTHETIC_NEEDS_HUMAN_REASONS = _FAIL_CLOSED_VERDICT_REASONS | frozenset(
    {"fixed_without_head_advance"}
)


def _is_synthetic_needs_human_reason(reason: str | None) -> bool:
    """Return whether ``reason`` is a parser/gate artifact, not a human decision."""
    return reason is not None and reason in _SYNTHETIC_NEEDS_HUMAN_REASONS


def _should_clear_salvage_for_parsed_verdict(
    *,
    verdict: Verdict,
    reason: str | None,
) -> bool:
    """Return whether an explicit non-FIXED claim must drop retained salvage."""
    return verdict in {"false_positive", "defer"} or (
        verdict == "needs_human" and reason not in _FAIL_CLOSED_VERDICT_REASONS
    )


async def _evaluate_local_head_advance(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
) -> tuple[str | None, bool, object, object]:
    """Return end HEAD, local advance flag, and ancestry helpers for reuse."""
    item_end_head: str | None = None
    rev_parse_end = getattr(runner, "_rev_parse_head", None)
    if callable(rev_parse_end) and worktree_path.exists():
        item_end_head = await rev_parse_end(worktree_path)

    descends = getattr(runner, "_head_descends_from", None)
    trees_differ = getattr(runner, "_commit_trees_differ", None)
    local_ancestry_evaluable = (
        item_start_head is not None
        and item_end_head is not None
        and callable(descends)
        and worktree_path.exists()
    )
    local_head_advanced = False
    if local_ancestry_evaluable:
        assert item_start_head is not None
        assert item_end_head is not None
        assert callable(descends)
        if item_end_head.lower() != item_start_head.lower() and await descends(
            worktree_path=worktree_path,
            ancestor=item_start_head,
            descendant=item_end_head,
        ):
            local_head_advanced = bool(
                callable(trees_differ)
                and await trees_differ(
                    worktree_path=worktree_path,
                    left=item_start_head,
                    right=item_end_head,
                )
            )
    return item_end_head, local_head_advanced, descends, trees_differ


async def _await_shield_preserving_cancellation(task: asyncio.Task[Any]) -> None:
    """Await ``task`` under shield; never let task failure replace CancelledError.

    Shield loops used to call ``task.result()`` once the salvage/persist work
    finished under cancel. A failed task re-raised from ``result()`` and dropped
    the saved cancel — on Python 3.11+ that consumes cancellation so a worker
    reload/shutdown can continue after salvage persistence errors
    (PRRT_kwDOSJAM6s6Zs01j). The same drop happens when cancel is saved while
    the task is still pending and a later ``shield`` await surfaces the task
    failure: catch that failure only after cancel was observed, retrieve it via
    ``exception()``, and chain it under CancelledError.
    """
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc
            if task.done():
                break
        except Exception:
            # Task finished failing after cancel was already observed. Prefer
            # re-raising cancel below rather than consuming it.
            if cancellation is not None:
                break
            raise
    if cancellation is None:
        return
    if task.done() and not task.cancelled():
        failure = task.exception()
        if failure is not None:
            raise cancellation from failure
    raise cancellation


async def _retain_failed_run_salvage_despite_cancellation(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    state: MonitorState | None,
    salvage_item_id: str | None,
    salvage_body_hash: str | None,
    isolated_worktree_host_path: Path | None,
    result_stdout: str,
) -> None:
    """Capture post-exit HEAD and retain salvage while an abort is pending.

    ``asyncio.CancelledError`` bypasses ``except Exception`` around the agent
    invocation and the dirty commit sink. On Python 3.11+, catching cancellation
    leaves it pending, so a direct post-cancel await would re-raise before
    salvage is recorded. Shield the evaluate+retain+persist work so a later
    no-change FIXED at the tip does not become ``fixed_without_head_advance``
    after a worker reload (PRRT_kwDOSJAM6s6Zmn1b, PRRT_kwDOSJAM6s6ZmsZQ,
    PRRT_kwDOSJAM6s6ZmviO — agent self-commit then cancel before the sink).

    The same retain+persist path is used when service-recovery / pre-launch
    guards raise ``ProviderRecoveryRetryError``,
    ``_MonitorAgentServiceRecoverySupersededError``, or ownership/mirror/head
    errors after a self-commit (PRRT_kwDOSJAM6s6Zm0PB) — those used bare re-raise
    and stranded salvage the same way.

    Persist ONLY the item's ``__salvaged_fix_*`` keys (merged onto DB state) —
    never full ``_persist_state``, which would flush mid-burst unconfirmed
    verdicts that ``CancelledError`` skips rolling back (PRRT_kwDOSJAM6s6Zmur3).

    After the shielded work finishes, re-raise any cancellation observed while
    awaiting the shield. Non-cancel callers (``ProviderRecoveryRetryError``,
    ownership/mirror/head) would otherwise re-raise the original error and drop
    the cancel (PRRT_kwDOSJAM6s6ZoX2e) — same save+re-raise contract as
    clarification cleanup and successful FIXED tip persist.
    """

    async def _capture_retain_and_persist() -> None:
        item_end_head, local_head_advanced, _, _ = await _evaluate_local_head_advance(
            runner,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
        )
        _retain_or_clear_failed_run_salvage(
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            local_head_advanced=local_head_advanced,
            item_end_head=item_end_head,
            item_start_head=item_start_head,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
            retain_for_failed_run=True,
        )
        # In-memory keys alone are lost when the monitor reloads from the DB after
        # cancellation (worker restart / new cycle). Persist only salvage keys
        # before the caller re-raises (PRRT_kwDOSJAM6s6ZmsZQ, PRRT_kwDOSJAM6s6Zmur3).
        if state is not None and salvage_item_id is not None:
            persist = getattr(runner, "_persist_failed_run_salvage_durably", None)
            if callable(persist):
                await persist(workspace_id, state, salvage_item_id=salvage_item_id)

    retain_task = asyncio.create_task(_capture_retain_and_persist())
    await _await_shield_preserving_cancellation(retain_task)


async def _await_failed_run_salvage_persist_shielded(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
    state: MonitorState,
    *,
    salvage_item_id: str,
) -> None:
    """Durable-write salvage keys even if ``CancelledError`` arrives mid-await.

    Bare ``await persist(...)`` lets cancel escape from exception handlers after
    in-memory retain, so a worker reload drops item-bound evidence while HEAD
    still carries the fix — a later no-change FIXED becomes
    ``fixed_without_head_advance`` (PRRT_kwDOSJAM6s6ZovMQ). Same shield +
    save/re-raise contract as successful-tip and salvage-clear paths.
    """
    persist = getattr(runner, "_persist_failed_run_salvage_durably", None)
    if not callable(persist):
        return
    persist_task = asyncio.create_task(
        persist(workspace_id, state, salvage_item_id=salvage_item_id)
    )
    await _await_shield_preserving_cancellation(persist_task)


def _retain_or_clear_failed_run_salvage(
    *,
    state: MonitorState | None,
    salvage_item_id: str | None,
    salvage_body_hash: str | None,
    local_head_advanced: bool,
    item_end_head: str | None,
    item_start_head: str | None,
    isolated_worktree_host_path: Path | None,
    result_stdout: str,
    retain_for_failed_run: bool,
) -> None:
    """Retain contentful failed-run salvage, or clear explicit non-FIXED claims.

    Isolated clarification checkouts are discarded on cleanup; retaining their tip
    would orphan ``__salvaged_fix_*`` keys on the primary worktree
    (PRRT_kwDOSJAM6s6ZmikP).
    """
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    def _clear() -> None:
        if state is not None and salvage_item_id is not None:
            state.threads_addressed_ids.pop(_salvaged_fix_head_state_key(salvage_item_id), None)
            state.threads_addressed_ids.pop(
                _salvaged_fix_body_hash_state_key(salvage_item_id), None
            )
            state.threads_addressed_ids.pop(_salvaged_fix_start_state_key(salvage_item_id), None)

    if (
        retain_for_failed_run
        and state is not None
        and salvage_item_id is not None
        and salvage_body_hash is not None
        and local_head_advanced
        and item_end_head is not None
        and item_start_head is not None
        and isolated_worktree_host_path is None
    ):
        state.mark_addressed(_salvaged_fix_head_state_key(salvage_item_id), item_end_head)
        state.mark_addressed(_salvaged_fix_body_hash_state_key(salvage_item_id), salvage_body_hash)
        state.mark_addressed(_salvaged_fix_start_state_key(salvage_item_id), item_start_head)

    if retain_for_failed_run:
        early_parsed = _parse_verdict_result(result_stdout)
        if _should_clear_salvage_for_parsed_verdict(
            verdict=early_parsed.verdict,
            reason=early_parsed.reason,
        ):
            _clear()


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reason: str | None = None


_CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED = "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
_CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED = "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
_CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED = "CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED"


async def _owned_paths_for_prompt(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    session_factory = runner._deps.session_factory
    session_context = session_factory()
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
) -> Verdict:
    return (
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
    ).verdict


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
    isolated_worktree_host_path: Path | None = None,
    isolated_worktree_ref: str | None = None,
    isolated_worktree_source_mirror: Path | None = None,
    read_only: bool = False,
    require_fix_evidence: bool = True,
    evidence_item_id: str | None = None,
    evidence_body_hash: str | None = None,
) -> VerdictResult:
    """Run the monitor agent and parse its verdict without losing reason details.

    ``require_fix_evidence`` (default True) enforces per-item HEAD/dirty
    evidence for FIXED claims. Operator hints pass False so GitHub-side /
    no-code directives can complete without a local commit.

    ``evidence_item_id`` scopes tip-evidence retention so a later successful
    FIXED retry for the same thread/comment can confirm a prior failed-run
    salvage or an ordinary successful tip that has not yet been pushed/resolved
    (HEAD still at the tip when the retry also started there, or descending
    from both the salvaged SHA and the retry start with the salvaged
    first-parent changes still present in the tip tree) without accepting a
    failed invocation's verdict. ``evidence_body_hash`` binds that evidence to
    the feedback body that produced it so an edited thread cannot reuse stale
    tip keys while ``agent_failed`` skips stale-body cleanup. Tip evidence is
    retained across FIXED accept and addressed-state clear until publication
    succeeds (PRRT_kwDOSJAM6s6ZnvBN). Evidence is never retained for
    ``isolated_worktree_host_path`` runs: those tips are discarded with the
    clarification checkout.
    """
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    result_stdout = ""
    cli_failed = False
    command_evidence: list[str] = []
    if state is not None:
        state.hosted_terminal_head_advanced = False
    salvage_item_id = (evidence_item_id or "").strip() or None
    salvage_body_hash = (evidence_body_hash or "").strip() or None
    if await runner._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    mirror_path: Path | None = None
    worktree_path = (
        isolated_worktree_host_path
        if isolated_worktree_host_path is not None
        else runner._worktrees_root / workspace_id
    )
    item_start_head = (operation_start_head or "").strip() or None
    if item_start_head is None and worktree_path.exists():
        rev_parse = getattr(runner, "_rev_parse_head", None)
        if callable(rev_parse):
            item_start_head = await rev_parse(worktree_path)
    if isolated_worktree_host_path is None:
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
            try:
                await repair_mirror_hooks_path(mirror_path)
            except (GitOperationError, OSError) as exc:
                repair_details = mirror_hooks_repair_failure_details(
                    exc,
                    repair_stage="before_comment_agent",
                    mirror_path=mirror_path,
                )
                _log.warning(
                    "monitor.mirror_hooks_path_repair_failed",
                    workspace_id=workspace_id,
                    reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                    **repair_details,
                )
                raise _MonitorMirrorHooksPathRepairFailedError() from exc
    agent_run_err = None
    try:
        if isolated_worktree_host_path is not None:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
                isolated_worktree_host_path=isolated_worktree_host_path,
                isolated_worktree_ref=isolated_worktree_ref,
                isolated_worktree_source_mirror=isolated_worktree_source_mirror,
            )
        elif read_only:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
                read_only=True,
            )
        else:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
            )
        result_stdout = result.stdout
    except AgentRunError as exc:
        if exc.reason_code in {
            _CLARIFICATION_MODEL_SERVICE_UPDATE_FAILED,
            _CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED,
            _CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED,
        }:
            raise _MonitorAgentServiceRecoveryFailedError(
                "clarification model service recovery failed",
                reason_code=exc.reason_code,
                details=exc.details,
            ) from exc
        cli_failed = True
        result_stdout = exc.result.stdout
        agent_run_err = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
    except (
        ProviderRecoveryRetryError,
        _MonitorAgentServiceRecoverySupersededError,
        _MonitorAgentServiceRecoveryFailedError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ):
        # Agent may have self-committed before service-recovery / pre-launch
        # guards exit. Bare re-raise previously skipped salvage so a later
        # no-change FIXED at the tip became fixed_without_head_advance
        # (PRRT_kwDOSJAM6s6Zm0PB). Retain+persist HEAD advance like CancelledError.
        await _retain_failed_run_salvage_despite_cancellation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
        )
        raise
    except Exception:
        if mirror_path is not None:
            try:
                await repair_mirror_hooks_path(mirror_path)
            except (GitOperationError, OSError) as exc:
                repair_details = mirror_hooks_repair_failure_details(
                    exc,
                    repair_stage="after_comment_agent_exception",
                    mirror_path=mirror_path,
                )
                _log.warning(
                    "monitor.mirror_hooks_path_repair_failed",
                    workspace_id=workspace_id,
                    reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                    **repair_details,
                )
                # Agent may have self-committed before the unexpected crash.
                # Sibling except handlers do not catch raises from this block, so
                # retain+persist HEAD advance before propagating poisoned hooks
                # (PRRT_kwDOSJAM6s6ZninJ) — otherwise a later no-change FIXED at
                # the tip becomes fixed_without_head_advance.
                await _retain_failed_run_salvage_despite_cancellation(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    state=state,
                    salvage_item_id=salvage_item_id,
                    salvage_body_hash=salvage_body_hash,
                    isolated_worktree_host_path=isolated_worktree_host_path,
                    result_stdout=result_stdout,
                )
                raise _MonitorMirrorHooksPathRepairFailedError() from exc
        # Unexpected agent crashes still try to sink dirty work. Capture post-sink
        # HEAD and retain salvage before re-raising — otherwise a later no-change
        # FIXED at the tip becomes fixed_without_head_advance (same gap as the
        # main sink path; PRRT_kwDOSJAM6s6ZmirT). Cancellation after the sink
        # commit must retain salvage the same way (PRRT_kwDOSJAM6s6Zmn1b).
        # In-memory keys alone are lost when the exception ends the monitor cycle
        # or triggers a worker reload — persist only ``__salvaged_fix_*`` keys
        # before re-raising (same selective merge as CancelledError;
        # PRRT_kwDOSJAM6s6Zmzxr).
        # Probe/retain/persist must run through the cancellation-safe helper:
        # CancelledError from an unshielded await inside this ``except Exception``
        # escapes the handler (sibling CancelledError clauses cannot catch it),
        # stranding HEAD advance without item-bound salvage
        # (PRRT_kwDOSJAM6s6Zo8Dt).
        unexpected_sink_error: BaseException | None = None
        if commit_dirty_changes:
            try:
                await runner._commit_dirty_worktree(
                    workspace_id=workspace_id,
                    message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    command_evidence=command_evidence,
                    task_tag=task_tag,
                    operation_start_head=operation_start_head,
                )
            except Exception as sink_exc:
                unexpected_sink_error = sink_exc
            except asyncio.CancelledError:
                await _retain_failed_run_salvage_despite_cancellation(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    item_start_head=item_start_head,
                    state=state,
                    salvage_item_id=salvage_item_id,
                    salvage_body_hash=salvage_body_hash,
                    isolated_worktree_host_path=isolated_worktree_host_path,
                    result_stdout=result_stdout,
                )
                raise
        await _retain_failed_run_salvage_despite_cancellation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
        )
        if unexpected_sink_error is not None:
            raise unexpected_sink_error from None
        raise
    except asyncio.CancelledError:
        # Agent may have self-committed before cancellation; retain HEAD advance
        # before re-raising so a worker reload does not strand the thread as
        # fixed_without_head_advance (PRRT_kwDOSJAM6s6ZmviO).
        await _retain_failed_run_salvage_despite_cancellation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
        )
        raise

    # Commit dirty changes, then evaluate HEAD / retain salvage even when the
    # sink itself raises after advancing HEAD (post-commit ownership repair, or
    # protected-scope repair self-commit then provider recovery). Recording only
    # on the success path drops salvage so a later no-change FIXED at the tip
    # becomes fixed_without_head_advance (PRRT_kwDOSJAM6s6ZmirT). Also retain
    # before ``_handle_provider_agent_run_error``, which persists then raises.
    # ``CancelledError`` bypasses ``except Exception``; retain before propagating
    # cancellation (PRRT_kwDOSJAM6s6Zmn1b). Service-recovery failed/superseded
    # from the sink must selectively durable-persist before re-raise — the outer
    # runner returns without ``_persist_state`` (PRRT_kwDOSJAM6s6Zm-Yt).
    commit_sink_error: BaseException | None = None
    committed_dirty_changes = False
    if commit_dirty_changes:
        try:
            committed_dirty_changes = bool(
                await runner._commit_dirty_worktree(
                    workspace_id=workspace_id,
                    message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    command_evidence=command_evidence,
                    task_tag=task_tag,
                    operation_start_head=operation_start_head,
                )
            )
        except Exception as sink_exc:
            commit_sink_error = sink_exc
        except asyncio.CancelledError:
            await _retain_failed_run_salvage_despite_cancellation(
                runner,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                item_start_head=item_start_head,
                state=state,
                salvage_item_id=salvage_item_id,
                salvage_body_hash=salvage_body_hash,
                isolated_worktree_host_path=isolated_worktree_host_path,
                result_stdout=result_stdout,
            )
            raise

    # Post-commit evidence capture (evaluate → retain → selective persist on sink
    # failure) must survive worker cancel the same way as sink/agent cancel paths.
    # An unshielded ``_evaluate_local_head_advance`` await after the sink returns
    # lets CancelledError escape before salvage keys are retained/persisted while
    # HEAD already carries the fix — a restart's no-change FIXED becomes
    # fixed_without_head_advance (PRRT_kwDOSJAM6s6Zoxg2).
    def _clear_retained_salvage() -> None:
        if state is not None and salvage_item_id is not None:
            state.threads_addressed_ids.pop(_salvaged_fix_head_state_key(salvage_item_id), None)
            state.threads_addressed_ids.pop(
                _salvaged_fix_body_hash_state_key(salvage_item_id), None
            )
            state.threads_addressed_ids.pop(_salvaged_fix_start_state_key(salvage_item_id), None)

    async def _clear_retained_salvage_durably() -> None:
        # In-memory pops alone are lost when settle-sleep cancel reloads DB state
        # before full ``_persist_state``. Persist selective deletions the same
        # way adds already do (PRRT_kwDOSJAM6s6Zn212).
        # Shield the write itself: cancel mid-await otherwise leaves DB
        # ``__salvaged_fix_*`` keys intact after in-memory pops, so a later
        # no-change FIXED can reuse evidence the explicit non-fix verdict
        # invalidated (PRRT_kwDOSJAM6s6ZohUm) — same contract as successful
        # tip persist (PRRT_kwDOSJAM6s6ZoXc9).
        _clear_retained_salvage()
        if state is not None and salvage_item_id is not None:
            persist = getattr(runner, "_persist_failed_run_salvage_durably", None)
            if callable(persist):
                persist_task = asyncio.create_task(
                    persist(workspace_id, state, salvage_item_id=salvage_item_id)
                )
                await _await_shield_preserving_cancellation(persist_task)

    try:
        (
            item_end_head,
            local_head_advanced,
            descends,
            trees_differ,
        ) = await _evaluate_local_head_advance(
            runner,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
        )
        local_ancestry_evaluable = (
            item_start_head is not None
            and item_end_head is not None
            and callable(descends)
            and worktree_path.exists()
        )
        # Hosted sync may set hosted_terminal_head_advanced; re-verify forward
        # ancestry of last_push_sha so a lateral/older remote rewrite cannot satisfy
        # FIXED via the flag after local ancestry correctly rejected the same move.
        # Empty hosted tips need the same tree-diff gate as local advances.
        hosted_head_advanced = False
        if state is not None and state.hosted_terminal_head_advanced:
            synced_head = (state.last_push_sha or "").strip() or None
            if (
                item_start_head is not None
                and synced_head is not None
                and synced_head.lower() != item_start_head.lower()
                and callable(descends)
                and worktree_path.exists()
                and await descends(
                    worktree_path=worktree_path,
                    ancestor=item_start_head,
                    descendant=synced_head,
                )
            ):
                hosted_head_advanced = bool(
                    callable(trees_differ)
                    and await trees_differ(
                        worktree_path=worktree_path,
                        left=item_start_head,
                        right=synced_head,
                    )
                )
        # Dirty commit is item-scoped evidence only for stub / missing-worktree
        # environments where ancestry cannot be evaluated. A present worktree
        # whose post-commit HEAD probe fails must fail closed — accepting
        # committed_dirty_changes as FIXED after a burst reset to the remote tip
        # can push a replacement commit while silently dropping an earlier item's
        # fix (PRRT_kwDOSJAM6s6ZpUC7). When both heads are known, ancestry (or
        # equal SHAs) is authoritative — never accept FIXED via dirty after a
        # known non-descendant move.
        dirty_fix_evidence = (
            bool(committed_dirty_changes)
            and not local_ancestry_evaluable
            and not worktree_path.exists()
        )
        item_fix_evidence = local_head_advanced or hosted_head_advanced or dirty_fix_evidence

        # Failed runs may still leave a contentful salvage commit. Retain that SHA
        # for this item so a later successful FIXED can confirm it when HEAD is
        # still at the salvage tip (and this retry started there) or descends from
        # both the salvage tip and the retry start with salvaged path changes still
        # present — without resolving from the failed invocation. Ancestry alone
        # would accept a descendant that reverts the salvage. Equality alone is too
        # strict in multi-item bursts: a later thread can advance HEAD past the
        # salvage while leaving it in history; equality without a matching start
        # would also accept a backward reset that discards that later tip. Bind the
        # feedback body hash so an edited thread cannot reuse salvage created for
        # prior feedback while agent_failed skips stale cleanup. Bind the invocation
        # start SHA so descendant reuse verifies the full start..salvage delta when
        # the failed run created multiple commits (PRRT_kwDOSJAM6s6ZmG-B).
        # A sink raise after HEAD advance is also a failed run for salvage purposes.
        retain_for_failed_run = cli_failed or commit_sink_error is not None
        _retain_or_clear_failed_run_salvage(
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            local_head_advanced=local_head_advanced,
            item_end_head=item_end_head,
            item_start_head=item_start_head,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
            retain_for_failed_run=retain_for_failed_run,
        )

        if commit_sink_error is not None:
            # In-memory salvage alone is lost when the outer runner catches
            # ``_MonitorAgentServiceRecoveryFailedError`` /
            # ``_MonitorAgentServiceRecoverySupersededError`` and returns without
            # ``_persist_state`` (runner.py). Persist only ``__salvaged_fix_*``
            # keys before re-raising — same selective merge as CancelledError /
            # unexpected Exception (PRRT_kwDOSJAM6s6Zm-Yt). Shield the write so
            # cancel mid-await cannot drop salvage evidence (PRRT_kwDOSJAM6s6ZovMQ).
            if state is not None and salvage_item_id is not None:
                await _await_failed_run_salvage_persist_shielded(
                    runner,
                    workspace_id,
                    state,
                    salvage_item_id=salvage_item_id,
                )
            raise commit_sink_error from None
    except asyncio.CancelledError:
        await _retain_failed_run_salvage_despite_cancellation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            state=state,
            salvage_item_id=salvage_item_id,
            salvage_body_hash=salvage_body_hash,
            isolated_worktree_host_path=isolated_worktree_host_path,
            result_stdout=result_stdout,
        )
        raise

    if agent_run_err is not None:
        # Persist (including salvage keys above) then maybe raise retry/fallback.
        await runner._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.cli_nonzero_exit",
            returncode=agent_run_err.result.returncode,
        )

    retained_salvage_evidence = False
    if (
        not item_fix_evidence
        and not cli_failed
        and state is not None
        and salvage_item_id is not None
        and salvage_body_hash is not None
        and item_end_head is not None
    ):
        retained_head = (
            state.threads_addressed_ids.get(_salvaged_fix_head_state_key(salvage_item_id)) or ""
        ).strip()
        retained_body = (
            state.threads_addressed_ids.get(_salvaged_fix_body_hash_state_key(salvage_item_id))
            or ""
        ).strip()
        retained_start = (
            state.threads_addressed_ids.get(_salvaged_fix_start_state_key(salvage_item_id)) or ""
        ).strip()
        if retained_head and retained_body and retained_body == salvage_body_hash:
            # Only reuse the salvage tip when this retry also started there;
            # otherwise require end to remain a descendant of both the retry
            # start and the retained salvage, with salvaged path changes still
            # present (ancestry alone accepts a revert of the salvage).
            end_matches_retained = retained_head.lower() == item_end_head.lower()
            if end_matches_retained:
                retained_salvage_evidence = (
                    item_start_head is not None and item_start_head.lower() == retained_head.lower()
                )
            elif not retained_start:
                # Legacy unbound start — cannot verify the full failed-run delta.
                _clear_retained_salvage()
            else:
                # Equality alone accepts H2→H1 resets that discard another item's
                # tip after item-start ancestry already rejected the backward move.
                changes_present = getattr(runner, "_commit_changes_present_in_head", None)
                start_ok = item_start_head is not None and (
                    item_start_head.lower() == item_end_head.lower()
                    or (
                        callable(descends)
                        and worktree_path.exists()
                        and await descends(
                            worktree_path=worktree_path,
                            ancestor=item_start_head,
                            descendant=item_end_head,
                        )
                    )
                )
                ancestry_ok = bool(
                    start_ok
                    and callable(descends)
                    and worktree_path.exists()
                    and await descends(
                        worktree_path=worktree_path,
                        ancestor=retained_head,
                        descendant=item_end_head,
                    )
                )
                content_ok = bool(
                    ancestry_ok
                    and callable(changes_present)
                    and await changes_present(
                        worktree_path=worktree_path,
                        commit=retained_head,
                        head=item_end_head,
                        baseline=retained_start,
                    )
                )
                if ancestry_ok and callable(changes_present) and not content_ok:
                    # Later commits undid the salvage; drop so it cannot linger.
                    _clear_retained_salvage()
                retained_salvage_evidence = content_ok
        elif retained_head or retained_body or retained_start:
            # Feedback moved on or legacy unbound salvage — drop so it cannot linger.
            _clear_retained_salvage()
    item_fix_evidence = item_fix_evidence or retained_salvage_evidence

    parsed = _parse_verdict_result(result_stdout)
    if parsed.verdict in {"false_positive", "defer"}:
        # Never upgrade an explicit non-fix marker because dirty or hosted head
        # advanced (strand prevention over guessing). But a nonzero CLI exit means
        # the run did not complete — do not resolve/defer from a pre-crash marker.
        # Drop any dirty salvage from this crash so a later no-change FIXED retry
        # cannot treat a non-FIXED attempt as item evidence.
        await _clear_retained_salvage_durably()
        if cli_failed:
            return VerdictResult(verdict="agent_failed")
        return parsed
    if parsed.verdict == "needs_human":
        # Keep explicit NEEDS_HUMAN (and successful fail-closed parses). Only map
        # fail-closed synthetic reasons to agent_failed when the CLI itself failed,
        # so crashes still retry instead of parking a human escalation.
        if cli_failed and parsed.reason in _FAIL_CLOSED_VERDICT_REASONS:
            return VerdictResult(verdict="agent_failed")
        # Explicit NEEDS_HUMAN is not fix evidence — clear salvage so a later
        # FIXED retry cannot reuse it. Preserve salvage for synthetic fail-closed
        # parses (markerless/garbled) so a later no-change FIXED can still prove
        # an already-present salvage (PRRT_kwDOSJAM6s6ZncGe).
        if _should_clear_salvage_for_parsed_verdict(
            verdict=parsed.verdict,
            reason=parsed.reason,
        ):
            await _clear_retained_salvage_durably()
        return parsed
    if parsed.verdict == "fix_committed":
        # Hosted recovery may sync a terminal SHA and set advance evidence before
        # re-raising AgentRunError; never resolve FIXED from a failed invocation.
        if cli_failed:
            return VerdictResult(verdict="agent_failed")
        if item_fix_evidence:
            # Keep tip evidence until push/resolve succeed. Clearing here (or
            # omitting ordinary successful-commit keys) converts a later
            # no-change FIXED after publication requeue into
            # fixed_without_head_advance (PRRT_kwDOSJAM6s6ZnvBN). Refresh keys
            # when this invocation advanced HEAD; leave prior salvage when the
            # accept only confirmed retained tip evidence.
            if (
                (local_head_advanced or hosted_head_advanced or dirty_fix_evidence)
                and state is not None
                and salvage_item_id is not None
                and salvage_body_hash is not None
                and item_end_head is not None
                and item_start_head is not None
                and isolated_worktree_host_path is None
            ):
                state.mark_addressed(_salvaged_fix_head_state_key(salvage_item_id), item_end_head)
                state.mark_addressed(
                    _salvaged_fix_body_hash_state_key(salvage_item_id),
                    salvage_body_hash,
                )
                state.mark_addressed(
                    _salvaged_fix_start_state_key(salvage_item_id), item_start_head
                )
            # Tip keys above (or retained from an earlier failed/successful run)
            # must survive settle-sleep cancel / worker reload before
            # ``_persist_state`` runs at the end of ``_execute``. Persist only
            # ``__salvaged_fix_*`` keys — same selective merge as failed/
            # cancelled paths — so a no-change FIXED retry still has evidence
            # (PRRT_kwDOSJAM6s6Znz-0). Skip when no head key is present (e.g.
            # isolated clarification tips that must not bind salvage).
            # Shield the write itself: cancel mid-await otherwise drops tip
            # evidence while HEAD already advanced (PRRT_kwDOSJAM6s6ZoXc9).
            if (
                state is not None
                and salvage_item_id is not None
                and state.threads_addressed_ids.get(_salvaged_fix_head_state_key(salvage_item_id))
            ):
                persist = getattr(runner, "_persist_failed_run_salvage_durably", None)
                if callable(persist):
                    persist_task = asyncio.create_task(
                        persist(workspace_id, state, salvage_item_id=salvage_item_id)
                    )
                    await _await_shield_preserving_cancellation(persist_task)
            return parsed
        if not require_fix_evidence:
            # Operator hints may finish with only GitHub-side work; the prompt
            # documents FIXED without a code change for that path.
            await _clear_retained_salvage_durably()
            return parsed
        reason = redact_audit_text("fixed_without_head_advance")
        return VerdictResult(verdict="needs_human", reason=reason or "fixed_without_head_advance")
    if cli_failed:
        return VerdictResult(verdict="agent_failed")
    await _clear_retained_salvage_durably()
    return parsed
