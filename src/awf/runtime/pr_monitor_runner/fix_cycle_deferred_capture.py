"""Durable capture of deferred review threads for ``fix_cycle`` (#305).

Kept separate so ``fix_cycle`` stays under the first-party line budget.

When the agent answers ``defer`` on an inline thread, the fix cycle may only
resolve that thread once the follow-up work exists somewhere durable. This
module owns that hand-off: the idempotency marker keyed by thread id *and* body
hash, the rendered conversation that becomes the tracking issue body, and the
capture itself (file the issue, post the explanatory comment, audit the
outcome).
"""

from __future__ import annotations

from typing import Any

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.feedback_policy import review_thread_body_hashes
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    ReviewThread,
    _review_thread_body_hash,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _defer_reason_state_key,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log


def _deferred_issue_filed_marker(thread_id: str, body_hash: str) -> str:
    """State key recording that a tracking issue was filed for a deferred thread.

    Distinct from the verdict/body-hash keys that ``_clear_addressed_state_by_id``
    pops, so the marker survives a resolve-retry's state clear and keeps the
    capture idempotent across outer monitor iterations (no duplicate issues).

    Keyed by the thread body hash as well as the id: a same-body resolve-retry
    stays idempotent, but if the thread later gains new reviewer replies the
    hash changes and the new feedback is captured into a fresh issue rather than
    silently resolved under the stale one.
    """
    return f"__deferred_issue_filed__:{thread_id}:{body_hash}"


def _deferred_issue_already_filed(state: MonitorState, thread: ReviewThread) -> bool:
    """True when a tracking issue was filed for this conversation (any hash era).

    Accepts markers keyed by the current content-only hash or either
    pre-normalize legacy form (ID-bearing or fallback null-id) so an in-flight
    resume does not file a duplicate — PRRT_kwDOSJAM6s6dfH8h /
    PRRT_kwDOSJAM6s6dfSq-.
    """
    return any(
        state.threads_addressed_ids.get(_deferred_issue_filed_marker(thread.thread_id, body_hash))
        for body_hash in review_thread_body_hashes(thread)
    )


def _deferred_thread_conversation(thread: ReviewThread) -> str:
    """Render the full review-bundle history for the tracking-issue body.

    A body-aware recapture (see ``_deferred_issue_filed_marker``) fires precisely
    because review evidence changed, so the filed issue must carry the associated
    review body and whole inline conversation — not just the truncated
    first-comment excerpt — or resolving the thread would lose deferred work.
    """
    blocks: list[str] = []
    if thread.review_context is not None:
        context = thread.review_context
        quoted = "\n".join(
            f"> {line}" for line in (context.body or context.body_excerpt).splitlines() or [""]
        )
        blocks.append(f"**Associated review body ({context.author or 'reviewer'})**:\n\n{quoted}")
    for comment in thread.comments:
        quoted = "\n".join(f"> {line}" for line in (comment.body or "").splitlines() or [""])
        blocks.append(f"**{comment.author or 'reviewer'}**:\n\n{quoted}")
    if not thread.comments:
        blocks.append(f"> {thread.body_excerpt}")
    return "\n\n".join(blocks)


async def _capture_deferred_review_thread(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    thread: ReviewThread,
    state: MonitorState,
    base_branch: str | None,
    remote_branch: str,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> bool | None:
    """Durably capture a follow-up ``defer`` before its thread is resolved (#305).

    Posts an explanatory PR comment and files a tracking issue. Idempotent per
    thread *and body*: a marker records that the issue was already filed so a
    later same-body resolve-retry (which clears the verdict and re-addresses the
    thread) does not file a duplicate, while new reviewer replies (a changed
    body) are captured into a fresh issue. Returns ``True`` when the deferred
    work is durably captured (caller may resolve the thread); ``False`` on a
    *permanent* capture failure (caller downgrades to ``needs_human`` so the
    merge stays blocked and the operator is notified); or ``None`` on a
    *transient* failure — the thread verdict is cleared so the next poll
    re-addresses and re-attempts capture once GitHub recovers, instead of
    permanently downgrading a valid defer.
    """
    marker = _deferred_issue_filed_marker(thread.thread_id, _review_thread_body_hash(thread))
    if _deferred_issue_already_filed(state, thread):
        return True
    location = thread.path or "the PR diff"
    thread_ref = thread.url or f"PR #{pr_number}"
    issue_title = f"Deferred from PR #{pr_number}: {location}"
    agent_reason = state.threads_addressed_ids.get(_defer_reason_state_key(thread.thread_id))
    agent_reason_section = (
        f"Agent's deferral reason:\n\n> {agent_reason}\n\n" if agent_reason else ""
    )
    issue_body = (
        f"AWF deferred a review thread while monitoring PR #{pr_number}.\n\n"
        f"- Path: {location}\n"
        f"- Thread: {thread_ref}\n\n"
        f"{agent_reason_section}"
        f"Review thread (full history):\n\n{_deferred_thread_conversation(thread)}\n\n"
        "This issue tracks the deferred follow-up so the PR thread could be "
        "resolved without losing the work."
    )
    try:
        issue_url = await self._deps.gh.create_issue(
            repo=repo,
            title=issue_title,
            body=issue_body,
        )
    except ForgeClientError as exc:
        # Both forges file the tracking issue through ``self._deps.gh``; either
        # raises a ``ForgeClientError`` subclass (e.g. a 403 when the token lacks
        # the issues-create scope, which Bitbucket cannot fall back to a comment).
        # Catching the shared base keeps a Bitbucket fault from escaping to the
        # runner's generic handler and terminating the monitor instead of
        # downgrading to ``needs_human``. Transient blips clear the verdict to
        # re-attempt next poll (``None``); permanent faults downgrade to
        # ``needs_human`` (``False``) so the merge gate keeps blocking and the
        # operator is notified. The transient-retry audit reason stays forge-specific.
        transient_retry_reason = (
            _BITBUCKET_TRANSIENT_RETRY_REASON
            if isinstance(exc, BitbucketClientError)
            else _GITHUB_TRANSIENT_RETRY_REASON
        )
        if await self._wait_after_transient_forge_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="capture_deferred_thread",
            state=state,
            monitor_log=monitor_log,
        ):
            # Transient (502 / rate-limit / reset): a temporary issue-API outage
            # must not permanently downgrade a valid defer to needs_human. Clear
            # the verdict so the next poll re-addresses and re-attempts capture
            # once the forge recovers. The thread stays unresolved meanwhile.
            _clear_addressed_state_by_id(state, thread.thread_id)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="capture_deferred_thread",
                outcome="requeued",
                reason_code=transient_retry_reason,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                evidence={"thread_ids": [thread.thread_id]},
            )
            return None
        # Permanent failure (e.g. token missing the issues scope). ``str(exc)``
        # already redacts; redact again defensively before logging/persisting.
        # ``redacted_detail()`` normalizes the human detail (gh stderr / Bitbucket
        # body) across forges.
        redacted_error = _redact_and_truncate_forge_error(str(exc))
        _log.warning(
            "monitor.deferred_capture_failed",
            thread_id=thread.thread_id,
            stderr=_redact_and_truncate_forge_error(exc.redacted_detail()),
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action="capture_deferred_thread",
            outcome="failed",
            reason_code="DEFERRED_CAPTURE_FAILED",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            evidence={"thread_ids": [thread.thread_id], "error_message": redacted_error},
        )
        return False
    # The tracking issue was filed: clear any stale ``capture_deferred_thread``
    # retry count so a recovered blip never accumulates toward the bounded budget.
    await self._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="capture_deferred_thread",
    )
    # Filing the tracking issue is the durable capture. Record it immediately so
    # a later retry (e.g. after a failed push) never files a duplicate, even if
    # the explanatory comment below fails. The comment is best-effort courtesy.
    state.mark_addressed(marker, issue_url)
    try:
        await self._deps.gh.post_comment(
            repo=repo,
            pr_number=pr_number,
            body=(
                f"AWF deferred the review thread on `{location}` and filed "
                f"{issue_url} to track the follow-up. Resolving this thread; the "
                "deferred work lives in that issue."
            ),
        )
    except ForgeClientError as exc:
        # The tracking issue is already filed and recorded; this explanatory
        # comment is best-effort courtesy, so a failure on either forge is
        # swallowed. Catching the shared base keeps a Bitbucket fault from escaping
        # and terminating the monitor after the durable capture is already done.
        # ``redacted_detail()`` normalizes the human detail across forges.
        _log.warning(
            "monitor.deferred_capture_comment_failed",
            thread_id=thread.thread_id,
            issue_url=issue_url,
            stderr=_redact_and_truncate_forge_error(exc.redacted_detail()),
        )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action="capture_deferred_thread",
        outcome="succeeded",
        reason_code="DEFERRED_CAPTURE",
        pr_number=pr_number,
        status=None,
        base_branch=base_branch or "",
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        evidence={"thread_ids": [thread.thread_id], "issue_url": issue_url},
    )
    return True
