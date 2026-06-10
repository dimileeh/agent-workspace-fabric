"""Resolve addressed review threads that became OUTDATED (#473).

When the PR monitor addresses a review thread by changing code ELSEWHERE (a
different file/line than the comment anchor), the forge marks the original thread
``isOutdated=true``. Both forge clients drop outdated threads from
``PRStatus.unresolved_inline_threads`` because they are non-blocking for merge,
so ``decide()`` sees zero unresolved threads and proceeds to ``Merge`` — correct.
But the fix-cycle resolve loop only iterates that actionable feed, so the
now-outdated thread is never resolved. The result is a merged PR with a lingering
"unresolved" comment even though the monitor recorded a fix verdict in
``state.threads_addressed_ids`` — eroding the operator-visible "did AWF handle
everything?" signal.

This focused step closes that gap forge-neutrally. The forge clients surface the
addressed-but-outdated threads in ``PRStatus.outdated_unresolved_inline_threads``;
here we resolve only the ones the monitor already recorded with a fix verdict.
``defer`` / ``needs_human`` / ``agent_failed`` threads legitimately stay open.
"""

from __future__ import annotations

from typing import Any

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    _mark_review_thread_addressed,
    _review_thread_needs_attention,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _RESOLVABLE_THREAD_VERDICTS
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.logging import _log

# Verdicts whose now-OUTDATED threads this hygiene step may resolve. This is the
# fix-cycle's ``_RESOLVABLE_THREAD_VERDICTS`` MINUS ``defer``: a defer's thread
# is only resolved after its follow-up work is durably captured (an explanatory
# comment + tracking issue), which the fix cycle gates on at address time. That
# capture cannot be re-verified here, so an outdated defer thread is left open
# rather than silently resolved without its tracking issue. The kept verdicts
# (``fix_committed`` / ``false_positive``) both mean "handled, thread should
# close, no human follow-up".
_OUTDATED_RESOLVABLE_THREAD_VERDICTS = _RESOLVABLE_THREAD_VERDICTS - frozenset({"defer"})


async def _seed_outdated_thread_verdicts_from_branch_evidence(
    self: Any,
    *,
    workspace_id: str,
    status: PRStatus,
    state: MonitorState,
) -> None:
    """Seed missing verdicts for outdated threads the PR branch already addressed (#484).

    After a re-adoption / instance handoff, an outdated thread a PRIOR instance
    addressed (via an in-place ``fix: address …`` commit) carries no verdict in
    THIS instance's ``state.threads_addressed_ids``. The #473 resolution below
    gates on a recorded verdict, and outdated threads are dropped from the
    actionable feed, so without one the thread can never be resolved and GitHub
    keeps the PR BLOCKED on an invisible (collapsed) conversation while the monitor
    reports ``unresolved_threads=0`` and loops on ``NotifyHuman``.

    Reconcile the verdict from durable branch evidence: a commit on the worktree
    HEAD whose message references BOTH the unique thread id (``PRRT_…``) AND the
    ``fix: address`` prefix — the two shapes AWF emits for review-thread fixes
    (``comments.py``: ``fix: address PR review thread <id>``; ``monitor_prompts.py``:
    ``fix: address <id> — …``). A match is strong proof THIS branch already fixed
    the feedback, so record ``fix_committed``. Use ``_mark_review_thread_addressed``
    (not bare ``state.mark_addressed``) so the current body-hash snapshot is stored
    too — otherwise the resolve loop's ``_review_thread_needs_attention`` guard
    would skip the freshly-seeded thread.

    Trade-off (accepted): the snapshot is taken HERE, at re-adoption time, not at
    the prior instance's fix time. So if a reviewer replied to the outdated thread
    AFTER that fix but BEFORE this re-adoption, the reply is already baked into the
    seeded hash and ``_review_thread_needs_attention`` can never fire for it — the
    post-fix reply is accepted as-is and the thread is resolved without being
    re-triaged. This is deliberate: the only alternative (not seeding) leaves the
    PR permanently BLOCKED on an invisible collapsed conversation. The guard still
    protects against replies that arrive AFTER seeding, on subsequent polls.

    Best-effort and self-contained: runs only for outdated threads lacking a
    verdict (no git call in steady state), uses a bounded ``git log -n 1`` grep,
    and leaves a thread untouched on a non-``ok`` git result or no match. Never
    raises.
    """
    unseeded = [
        thread
        for thread in status.outdated_unresolved_inline_threads
        if state.threads_addressed_ids.get(thread.thread_id) is None
    ]
    if not unseeded:
        return
    worktree_path = self._worktrees_root / workspace_id
    for thread in unseeded:
        # ``-F --all-match`` requires BOTH literal substrings (the unique thread id
        # AND ``fix: address``) present in one commit message — matching both AWF
        # commit shapes while staying specific enough not to seed a thread the
        # branch never addressed.
        result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "log",
                "-n",
                "1",
                "--format=%H",
                "-F",
                "--all-match",
                "--grep",
                thread.thread_id,
                "--grep",
                "fix: address",
                "HEAD",
            )
        )
        if result.ok and result.stdout.strip():
            _mark_review_thread_addressed(state, thread, "fix_committed")


async def _resolve_addressed_outdated_threads(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str | None,
    remote_branch: str | None,
    monitor_log: WorkspaceLogSink | None = None,
) -> None:
    """Resolve threads the monitor addressed that have since gone OUTDATED (#473).

    Iterates ``status.outdated_unresolved_inline_threads`` and resolves only the
    threads whose latest recorded verdict is in
    ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``. Error handling mirrors the fix-cycle
    resolve loop and is fully self-contained so a forge fault never escapes into
    the monitor's outer loop: a transient fault waits then leaves the thread for
    the next poll (the resolvable verdict is preserved so it is retried, and the
    thread is flagged requeued so ``decide`` — which runs this same iteration —
    blocks merge instead of merging over it before the retry runs); a permanent
    fault is logged + audited and the verdict is downgraded to ``needs_human`` so
    the next poll skips it instead of re-issuing the same failing resolve every
    cycle (a retry storm against a non-fixable fault). Neither path wedges
    auto-merge: a persistently transient fault eventually exhausts its retry budget
    and escalates to the permanent ``needs_human`` path, and a successful resolve
    clears the flag. A successful resolve otherwise needs no ``state`` mutation: the
    thread drops out of the outdated feed on the next fetch. The permanent-fault
    downgrade IS persisted before returning, so it survives a subsequent transient
    ``_execute`` fault (which skips ``_persist_state``); the transient requeue flag
    is in-memory only, matching the rest of the transient path.
    """
    del repo  # repo is recovered from the neutral thread_id by the forge client
    # #484: seed verdicts for outdated threads a prior instance already addressed
    # (re-adoption / handoff) from durable branch evidence, so the resolve loop
    # below — which gates on a recorded verdict — can resolve them this iteration
    # instead of leaving the PR permanently BLOCKED on an invisible conversation.
    await _seed_outdated_thread_verdicts_from_branch_evidence(
        self,
        workspace_id=workspace_id,
        status=status,
        state=state,
    )
    for thread in status.outdated_unresolved_inline_threads:
        tid = thread.thread_id
        if state.threads_addressed_ids.get(tid) not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS:
            continue
        # Mirror the fix-cycle's stale-thread guard (#305): an outdated thread can
        # still gain fresh reviewer replies after its verdict was recorded, which
        # change its body hash. Resolving it here would close feedback the monitor
        # never re-handled — and because outdated threads are dropped from the
        # actionable feed, the fix cycle never re-addresses them either. Leave such
        # a thread open AND let ``decide()`` block auto-merge on it:
        # ``_outdated_thread_has_fresh_feedback`` matches this exact condition
        # (closed verdict + changed body) so the new feedback is surfaced to a
        # human via ``NotifyHuman`` instead of being silently merged over.
        if _review_thread_needs_attention(state, thread):
            continue
        try:
            await self._deps.gh.resolve_thread(thread_id=tid)
        except ForgeClientError as exc:
            # Both forges resolve threads through ``self._deps.gh`` (GitHub or
            # Bitbucket), each raising a ``ForgeClientError`` subclass. Catching
            # the shared base keeps either fault from escaping into the runner's
            # poll loop. Transient blips wait and leave the thread for the next
            # poll (already-resolved races are tolerated by the same broad catch
            # — idempotent, never surfaced as a hard failure). The transient and
            # permanent audit reason codes stay forge-specific.
            transient_retry_reason = (
                _BITBUCKET_TRANSIENT_RETRY_REASON
                if isinstance(exc, BitbucketClientError)
                else _GITHUB_TRANSIENT_RETRY_REASON
            )
            if await self._wait_after_transient_forge_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="resolve_outdated_thread",
                monitor_log=monitor_log,
            ):
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_outdated_thread",
                    outcome="requeued",
                    reason_code=transient_retry_reason,
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 0,
                        "requeued_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                # Flag the thread requeued so ``decide`` (which runs THIS iteration,
                # right after this step) blocks merge on it instead of merging over
                # the addressed-but-unresolved outdated thread before the promised
                # next-poll retry runs. The fix verdict stays intact so the retry
                # still fires; the flag only gates merge until it lands. Set
                # in-memory only — like the rest of the transient path, this is not
                # persisted, so the next poll reloads clean state and re-attempts the
                # resolve (which re-sets the flag if it faults again, or clears it on
                # success). A persistently transient fault eventually exhausts the
                # retry budget and escalates to the permanent ``needs_human`` path,
                # which keeps blocking — so this never silently merges over the
                # thread, and never wedges either.
                state.threads_addressed_ids[_outdated_resolve_requeued_key(tid)] = "requeued"
                continue
            # Permanent fault: log + audit, then downgrade the recorded verdict to
            # ``needs_human`` so the next poll SKIPS this thread (``needs_human`` is
            # not in ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``) instead of re-issuing
            # a known-permanent resolve every cycle. Keeping the resolvable verdict
            # would retry the same failing forge call on every poll until the PR
            # merges — burning API quota and spamming logs for no benefit, since the
            # fault is permanent. This mirrors the fix-cycle's permanent-task path
            # (a non-fixable resolve fault escalates rather than retrying forever).
            # The thread is OUTDATED and dropped from the actionable feed, but the
            # ``needs_human`` downgrade DOES block auto-merge: ``decide``'s outdated
            # gate treats an outdated ``needs_human`` thread as a merge blocker
            # (``NotifyHuman``), so the unresolved-but-handled thread surfaces to an
            # operator instead of being silently merged over. ``redacted_detail()``
            # normalizes the human detail across forges (gh stderr / Bitbucket body).
            _log.warning(
                "monitor.resolve_outdated_thread_failed",
                thread_id=tid,
                stderr=exc.redacted_detail(),
            )
            state.mark_addressed(tid, "needs_human")
            # Clear any prior transient requeue flag for this thread: ``needs_human``
            # now blocks merge on its own, so the flag is redundant and would only
            # leave stale state behind.
            state.threads_addressed_ids.pop(_outdated_resolve_requeued_key(tid), None)
            # Persist the downgrade immediately. This step runs BEFORE ``decide`` /
            # ``_execute`` in the runner loop, and ``_execute`` skips ``_persist_state``
            # when it hits a transient ``ForgeClientError`` (continuing to the next
            # poll, which reloads clean state from the DB). Without persisting here the
            # in-memory ``needs_human`` downgrade would be lost on that path, and the
            # next poll would re-issue the same known-permanent resolve — the exact
            # retry storm this downgrade exists to stop. The mutation is safe to flush
            # now: state was loaded fresh this iteration and carries no unconfirmed
            # fix-cycle markers yet (those only appear inside ``_execute``).
            await self._persist_state(workspace_id, state)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_outdated_thread",
                outcome="needs_human",
                reason_code=exc.reason_code,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                evidence={
                    "thread_ids": [tid],
                    "resolved_thread_count": 0,
                    "needs_human_thread_count": 1,
                    "error_message": str(exc),
                },
            )
            continue
        # Resolve landed — clear any transient requeue flag from a prior poll so
        # ``decide`` no longer holds this thread at ``NotifyHuman``.
        state.threads_addressed_ids.pop(_outdated_resolve_requeued_key(tid), None)
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action="resolve_outdated_thread",
            outcome="succeeded",
            reason_code="COMMENT_REPAIR",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            evidence={
                "thread_ids": [tid],
                "resolved_thread_count": 1,
            },
        )
