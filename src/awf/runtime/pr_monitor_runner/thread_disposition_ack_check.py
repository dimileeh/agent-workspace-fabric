"""Runner step: the one-time agent check of a bot reply after AWF's marker (#1138, D1).

The disposition gate flags a thread whose opening bot replied after AWF's
disposition (:mod:`~.thread_disposition_ack`) and re-polls. This step runs on the
next poll, before ``decide()``, where no merge lock is held. For each flagged
thread it re-reads that one thread, and for each of the bot's comments with no
judgment yet it runs a judgment-only agent check. The judgment (``ACKNOWLEDGES``,
``NEW_POINT``, or ``UNJUDGED`` when the reply is too long to quote whole, or the
agent failed, answered unusably or changed the worktree) is persisted at once, so a restart cannot re-run it, and
gets one reason-coded log line and one audit event without any comment body. The
check runs only on a clean worktree, and anything the agent changed there is
rolled back at once, so the fallback's repair can start. A
check is recorded ``UNJUDGED`` before the agent starts, so even a crash mid-run
cannot repeat it; only a provider failure, which goes to the monitor's provider
recovery like every other agent run, leaves it pending for a later poll. A
thread whose re-read keeps failing gives up after
:data:`~.thread_disposition_ack.MAX_ACK_REFETCH_FAILURES` polls.
AWF posts no reply and reopens nothing here: the gate's next audit certifies an
acknowledgement, and anything else takes the bounded fallback reopen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.adapters.base import AgentRunError
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedThreadComment
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _disposition_ack_key, _disposition_ack_pending_key
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.path_parsing import _untracked_paths_from_porcelain_z
from awf.runtime.pr_monitor_runner.thread_disposition import (
    REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED,
    REVIEW_THREAD_DISPOSITION_ACK_NEW_POINT,
    REVIEW_THREAD_DISPOSITION_ACKNOWLEDGED,
)
from awf.runtime.pr_monitor_runner.thread_disposition_ack import (
    ACKNOWLEDGES,
    MAX_ACK_REFETCH_FAILURES,
    NEW_POINT,
    UNJUDGED,
    AckJudgment,
    ack_check_prompt,
    ack_refetch_failures,
    ack_reply_fits,
    acknowledgement_view,
    comment_identity,
    parse_ack_answer,
    read_ack_judgment,
    record_ack_judgment,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
)

_AUDIT_ACTION = "review_thread_disposition_ack_check"
_LOG_SOURCE = "disposition_ack"
_PROBE_TIMEOUT_SECONDS = 60.0
# Provider recovery owns these: not a judgment of the reply, so nothing is recorded.
_PROVIDER_RECOVERY_ERRORS = (
    ProviderRecoveryRetryError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryAuthError,
)
# The agent run failed for an infrastructure reason: a failed check (``UNJUDGED``).
_AGENT_INFRASTRUCTURE_ERRORS = (
    ComposeExecCleanupError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    TimeoutError,
)
_OUTCOMES: dict[str, tuple[str, str]] = {
    ACKNOWLEDGES: ("acknowledged", REVIEW_THREAD_DISPOSITION_ACKNOWLEDGED),
    NEW_POINT: ("new_point", REVIEW_THREAD_DISPOSITION_ACK_NEW_POINT),
    UNJUDGED: ("check_failed", REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED),
}


async def judge_pending_disposition_acks(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    state: MonitorState,
    compose_project: str,
    compose_file: Path,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """Judge every bot reply the last gate flagged; a no-op when it flagged none."""
    prefix = _disposition_ack_pending_key("")
    flagged = sorted(k[len(prefix) :] for k in state.threads_addressed_ids if k.startswith(prefix))
    for thread_id in flagged:
        try:
            audit = await self._deps.gh.fetch_review_thread_audit(
                repo=repo, pr_number=pr_number, thread_id=thread_id
            )
        except ForgeClientError as exc:
            # Not a judgment: the check stays pending for the next poll, up to the
            # cap; the gate then records it failed and the fallback re-triages.
            failures = ack_refetch_failures(state, thread_id) + 1
            state.mark_addressed(_disposition_ack_pending_key(thread_id), str(failures))
            _log.warning(
                "monitor.review_thread_disposition_ack_refetch_failed",
                workspace_id=workspace_id,
                pr_number=pr_number,
                thread_id=thread_id,
                refetch_failures=failures,
                reason_code=REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED,
                forge_reason_code=exc.reason_code,
                detail=_redact_and_truncate_forge_error(str(exc)),
            )
            if failures >= MAX_ACK_REFETCH_FAILURES:
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action=_AUDIT_ACTION,
                    outcome=_OUTCOMES[UNJUDGED][0],
                    reason_code=REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED,
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                    evidence={"thread_id": thread_id, "detail": "refetch_failed"},
                )
            continue
        viewer = (audit.viewer_login or "").strip().casefold()
        thread = next((t for t in audit.threads if t.thread_id == thread_id), None)
        view = acknowledgement_view(thread, viewer=viewer) if thread and viewer else None
        if thread is not None and view is not None:
            for comment in thread.trailing_comments:
                if read_ack_judgment(state, thread_id, comment) is not None:
                    continue
                # Recorded failed before the agent starts: at most one check, crash or not.
                record_ack_judgment(state, thread_id, comment, UNJUDGED)
                await self._persist_state(workspace_id, state)
                try:
                    judgment, detail = await _judge(
                        self,
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        state=state,
                        reply=comment.body,
                        prompt=ack_check_prompt(
                            thread.opener.body if thread.opener else "",
                            view.viewer_comment_bodies[-1],
                            comment.body,
                        ),
                    )
                except _PROVIDER_RECOVERY_ERRORS:
                    ack_key = _disposition_ack_key(thread_id, comment_identity(comment))
                    state.threads_addressed_ids.pop(ack_key, None)
                    await self._persist_state(workspace_id, state)
                    raise
                record_ack_judgment(state, thread_id, comment, judgment)
                # Saved before any further forge call or audit write: at most one check.
                await self._persist_state(workspace_id, state)
                await _record_judgment(
                    self,
                    thread_id=thread_id,
                    comment=comment,
                    judgment=judgment,
                    detail=detail,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                )
                if judgment != ACKNOWLEDGES:
                    break  # the thread takes the fallback whatever its later comments say
        # Judged, or no longer eligible: the gate's next audit decides.
        state.threads_addressed_ids.pop(_disposition_ack_pending_key(thread_id), None)


async def _judge(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState,
    reply: str,
    prompt: str,
) -> tuple[AckJudgment, str]:
    """Run the judgment-only agent check; return the judgment and why."""
    if not ack_reply_fits(reply):
        # The prompt would cut the reply off: a new point past the cap would go unseen.
        return UNJUDGED, "reply_truncated"
    worktree = self._worktrees_root / workspace_id
    before = await _worktree_fingerprint(self, worktree)
    if before is None:
        return UNJUDGED, "worktree_probe_failed"
    if before[1]:
        # A rollback could not keep these apart from the agent's edits: a staged or
        # edited untracked file would be lost or go unseen. Do not run it.
        return UNJUDGED, "worktree_dirty"
    try:
        result = await self._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=prompt,
            log_source=_LOG_SOURCE,
            state=state,
        )
    except AgentRunError as exc:
        await _restore_worktree(self, workspace_id, worktree, before)
        # Provider recovery first, as for every monitor agent run: it raises a
        # ProviderRecovery* error for a 429 / quota / auth failure.
        await self._handle_provider_agent_run_error(workspace_id, exc)
        return UNJUDGED, f"agent_failed:{exc.reason_code}"
    except _AGENT_INFRASTRUCTURE_ERRORS as exc:
        await _restore_worktree(self, workspace_id, worktree, before)
        return UNJUDGED, f"agent_failed:{type(exc).__name__}"
    if await _restore_worktree(self, workspace_id, worktree, before):
        # A judgment-only run must not touch the branch; its answer is not trusted.
        return UNJUDGED, "worktree_changed"
    answer = parse_ack_answer(result.stdout)
    if answer is None:
        return UNJUDGED, "unparseable_answer"
    return answer, "judged"


async def _worktree_fingerprint(self: Any, worktree: Path) -> tuple[str, str] | None:
    """HEAD and ``git status --porcelain -z`` of the worktree; ``None`` if a probe failed."""
    outputs: list[str] = []
    for args in (
        ("rev-parse", "HEAD"),
        # Every untracked leaf, so a file added under an untracked directory shows.
        ("status", "--porcelain", "-z", "--untracked-files=all"),
    ):
        try:
            result = await self._deps.runner.run(
                git_worktree_command(worktree, *args), timeout_seconds=_PROBE_TIMEOUT_SECONDS
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        outputs.append(result.stdout)
    return outputs[0], outputs[1]


async def _restore_worktree(
    self: Any, workspace_id: str, worktree: Path, before: tuple[str, str]
) -> bool:
    """Roll back whatever the check's agent changed; return whether it changed anything.

    ``before`` is clean, so resetting to its HEAD discards only the agent's
    commits and edits, and every untracked path left is one the agent added.
    """
    after = await _worktree_fingerprint(self, worktree)
    if after == before:
        return False
    restored = after is not None and await _git_ok(
        self, worktree, "reset", "--hard", before[0].strip()
    )
    if restored and after is not None:
        added = sorted(_untracked_paths_from_porcelain_z(after[1]))
        if added:
            restored = await _git_ok(
                self, worktree, "--literal-pathspecs", "clean", "-ffd", "--", *added
            )
    if not (restored and await _worktree_fingerprint(self, worktree) == before):
        # Fail closed: the fallback's repair then refuses the dirty worktree.
        _log.warning(
            "monitor.review_thread_disposition_ack_worktree_restore_failed",
            workspace_id=workspace_id,
            reason_code=REVIEW_THREAD_DISPOSITION_ACK_CHECK_FAILED,
        )
    return True


async def _git_ok(self: Any, worktree: Path, *args: str) -> bool:
    """Run one git command in the worktree; whether it succeeded."""
    try:
        result = await self._deps.runner.run(
            git_worktree_command(worktree, *args), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
    except OSError:
        return False
    return bool(result.returncode == 0)


async def _record_judgment(
    self: Any,
    *,
    thread_id: str,
    comment: AuditedThreadComment,
    judgment: AckJudgment,
    detail: str,
    workspace_id: str,
    pr_number: int,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """One log line and one audit event per judgment; never a comment body."""
    outcome, reason_code = _OUTCOMES[judgment]
    evidence = {
        "thread_id": thread_id,
        "comment_id": comment.comment_id,
        "comment_identity": comment_identity(comment),
        "detail": detail,
    }
    log = _log.info if judgment == ACKNOWLEDGES else _log.warning
    log(
        "monitor.review_thread_disposition_ack_check",
        workspace_id=workspace_id,
        pr_number=pr_number,
        outcome=outcome,
        reason_code=reason_code,
        **evidence,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=_AUDIT_ACTION,
        outcome=outcome,
        reason_code=reason_code,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        evidence=evidence,
    )
