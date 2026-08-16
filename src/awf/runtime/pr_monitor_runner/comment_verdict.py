"""CLI verdict and owned-path operations for PR comment handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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

    ``evidence_item_id`` scopes dirty-salvage retention so a later successful
    FIXED retry for the same thread/comment can confirm a prior failed-run
    salvage (HEAD still at the salvaged tip when the retry also started there,
    or descending from both the salvaged SHA and the retry start) without
    accepting that failed invocation's verdict. ``evidence_body_hash`` binds
    that salvage to the feedback body that produced it so an edited thread
    cannot reuse stale salvage while ``agent_failed`` skips stale-body cleanup.
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
    except (ProviderRecoveryRetryError, _MonitorAgentServiceRecoverySupersededError):
        raise
    except _MonitorAgentServiceRecoveryFailedError:
        raise
    except (
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ):
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
                raise _MonitorMirrorHooksPathRepairFailedError() from exc
        if commit_dirty_changes:
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
        raise

    committed_dirty_changes = (
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
        if commit_dirty_changes
        else False
    )

    if agent_run_err is not None:
        await runner._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.cli_nonzero_exit",
            returncode=agent_run_err.result.returncode,
        )

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
        # Narrow for type checkers; guarded by local_ancestry_evaluable above.
        assert item_start_head is not None
        assert item_end_head is not None
        assert callable(descends)
        # SHA inequality alone accepts resets/checkouts to older tips.
        # Require forward-only ancestry when evaluable; dirty evidence must
        # not override a known non-descendant move. Forward ancestry alone
        # still accepts empty commits (unchanged tree); require a tree diff.
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
    # Dirty commit is item-scoped evidence only when local HEAD/ancestry cannot
    # be evaluated (stub runners / missing worktree / missing heads). When both
    # heads are known, ancestry (or equal SHAs) is authoritative — never accept
    # FIXED via dirty after a known non-descendant move.
    dirty_fix_evidence = bool(committed_dirty_changes) and not local_ancestry_evaluable
    item_fix_evidence = local_head_advanced or hosted_head_advanced or dirty_fix_evidence

    # Failed runs may still leave a contentful salvage commit. Retain that SHA
    # for this item so a later successful FIXED can confirm it when HEAD is
    # still at the salvage tip (and this retry started there) or descends from
    # both the salvage tip and the retry start — without resolving from the
    # failed invocation. Equality alone is too strict in multi-item bursts:
    # a later thread can advance HEAD past the salvage while leaving it in
    # history; equality without a matching start would also accept a backward
    # reset that discards that later tip. Bind the feedback body hash so an
    # edited thread cannot reuse salvage created for prior feedback while
    # agent_failed skips stale cleanup.
    def _clear_retained_salvage() -> None:
        if state is not None and salvage_item_id is not None:
            state.threads_addressed_ids.pop(_salvaged_fix_head_state_key(salvage_item_id), None)
            state.threads_addressed_ids.pop(
                _salvaged_fix_body_hash_state_key(salvage_item_id), None
            )

    if (
        cli_failed
        and state is not None
        and salvage_item_id is not None
        and salvage_body_hash is not None
        and local_head_advanced
        and item_end_head is not None
    ):
        state.mark_addressed(_salvaged_fix_head_state_key(salvage_item_id), item_end_head)
        state.mark_addressed(_salvaged_fix_body_hash_state_key(salvage_item_id), salvage_body_hash)

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
        if retained_head and retained_body and retained_body == salvage_body_hash:
            # Equality alone accepts H2→H1 resets that discard another item's
            # tip after item-start ancestry already rejected the backward move.
            # Only reuse the salvage tip when this retry also started there;
            # otherwise require end to remain a descendant of both the retry
            # start and the retained salvage.
            end_matches_retained = retained_head.lower() == item_end_head.lower()
            if end_matches_retained:
                retained_salvage_evidence = (
                    item_start_head is not None and item_start_head.lower() == retained_head.lower()
                )
            else:
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
                retained_salvage_evidence = bool(
                    start_ok
                    and callable(descends)
                    and worktree_path.exists()
                    and await descends(
                        worktree_path=worktree_path,
                        ancestor=retained_head,
                        descendant=item_end_head,
                    )
                )
        elif retained_head or retained_body:
            # Feedback moved on or legacy unbound salvage — drop so it cannot linger.
            _clear_retained_salvage()
    item_fix_evidence = item_fix_evidence or retained_salvage_evidence

    parsed = _parse_verdict_result(result_stdout)
    if parsed.verdict in {"false_positive", "defer"}:
        # Never upgrade an explicit non-fix marker because dirty or hosted head
        # advanced (strand prevention over guessing). But a nonzero CLI exit means
        # the run did not complete — do not resolve/defer from a pre-crash marker.
        if cli_failed:
            return VerdictResult(verdict="agent_failed")
        _clear_retained_salvage()
        return parsed
    if parsed.verdict == "needs_human":
        # Keep explicit NEEDS_HUMAN (and successful fail-closed parses). Only map
        # fail-closed synthetic reasons to agent_failed when the CLI itself failed,
        # so crashes still retry instead of parking a human escalation.
        if cli_failed and parsed.reason in _FAIL_CLOSED_VERDICT_REASONS:
            return VerdictResult(verdict="agent_failed")
        if not cli_failed:
            _clear_retained_salvage()
        return parsed
    if parsed.verdict == "fix_committed":
        # Hosted recovery may sync a terminal SHA and set advance evidence before
        # re-raising AgentRunError; never resolve FIXED from a failed invocation.
        if cli_failed:
            return VerdictResult(verdict="agent_failed")
        if item_fix_evidence:
            _clear_retained_salvage()
            return parsed
        if not require_fix_evidence:
            # Operator hints may finish with only GitHub-side work; the prompt
            # documents FIXED without a code change for that path.
            _clear_retained_salvage()
            return parsed
        reason = redact_audit_text("fixed_without_head_advance")
        return VerdictResult(verdict="needs_human", reason=reason or "fixed_without_head_advance")
    if cli_failed:
        return VerdictResult(verdict="agent_failed")
    _clear_retained_salvage()
    return parsed
