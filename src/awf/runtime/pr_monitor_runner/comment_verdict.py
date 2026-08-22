"""Provider-neutral CLI verdict and evidence operations for PR comments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

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
) -> VerdictResult:
    """Run one logical item with at most one protocol-correction attempt.

    Provider execution/recovery errors are outside the protocol retry budget.
    Both protocol attempts share the item-start HEAD, so a commit made by the
    first attempt remains valid evidence for a corrected second response.
    ``evidence_item_id`` and ``evidence_body_hash`` remain accepted at the API
    boundary for call-site compatibility; no evidence is persisted or salvaged
    across process restarts.
    """
    del evidence_item_id, evidence_body_hash
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    worktree_path = runner._worktrees_root / workspace_id
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
    if state is not None:
        state.hosted_terminal_head_advanced = False

    for protocol_attempt in range(2):
        dirty_changes_committed = False
        if await runner._provider_recovery_suppresses_cli(workspace_id):
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
            if commit_dirty_changes:
                await runner._commit_dirty_worktree(
                    workspace_id=workspace_id,
                    message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    command_evidence=command_evidence,
                    task_tag=task_tag,
                    operation_start_head=item_start_head,
                )
            await runner._handle_provider_agent_run_error(workspace_id, exc, state=state)
            raise AgentVerdictExecutionError(reason_code=exc.reason_code) from exc
        except (
            ProviderRecoveryRetryError,
            _MonitorAgentServiceRecoverySupersededError,
            _MonitorAgentServiceRecoveryFailedError,
            _MonitorAgentRuntimeOwnershipRepairFailedError,
            _MonitorHeadObjectMissingError,
            _MonitorMirrorHooksPathRepairFailedError,
        ):
            raise
        except Exception:
            if mirror_path is not None:
                await _repair_mirror_hooks_or_raise(
                    workspace_id=workspace_id,
                    mirror_path=mirror_path,
                    stage="after_comment_agent_exception",
                )
            if commit_dirty_changes:
                await runner._commit_dirty_worktree(
                    workspace_id=workspace_id,
                    message=commit_message,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    command_evidence=command_evidence,
                    task_tag=task_tag,
                    operation_start_head=item_start_head,
                )
            raise

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

        logical_fix_evidence = logical_fix_evidence or await _item_fix_evidence(
            runner,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            state=state,
            dirty_changes_committed=dirty_changes_committed,
        )

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
                return parsed

        assert protocol_error is not None
        if protocol_attempt == 1:
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

    raise AssertionError("unreachable verdict retry state")


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
    state: MonitorState | None,
    dirty_changes_committed: bool,
) -> bool:
    """Verify a contentful forward change from the logical item start."""
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
    if not (callable(descends) and callable(trees_differ) and worktree_path.exists()):
        # Lightweight/mocked runners may not expose Git ancestry helpers. A
        # successful dirty-worktree sink is still scoped to this invocation;
        # the production runner always takes the stronger ancestry branch.
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
        return True
    return False
