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

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    ReviewThread,
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


def _thread_identifier_set(thread: ReviewThread) -> set[str]:
    """All ids an outdated thread's fix could have been recorded under (#547).

    A GitHub inline comment surfaces in AWF as BOTH a ``ReviewThread`` (carrying
    ``thread_id``, e.g. ``PRRT_…``) AND — when the fix-cycle took the COMMENT
    path rather than the thread path — a verdict keyed by the comment's
    ``databaseId`` (``comment_id``, e.g. ``4688598838``; the commit reads
    ``fix: address review comment issue:4688598838 — …``). The outdated-thread
    resolution keys on ``thread_id`` only, so a comment-path fix is invisible to
    it. The thread already carries its comment ids, so reconcile on the READ
    side: build the full identifier set and test the verdict store / branch
    evidence against ALL of them.

    ``None`` comment ids are filtered out; the set de-dups naturally. A thread
    with no comment ids falls back to ``{thread_id}`` (existing behavior).
    """
    ids = {thread.thread_id}
    ids.update(comment.comment_id for comment in thread.comments if comment.comment_id)
    return ids


def _grep_id_pattern(identifier: str) -> str:
    """An ``-E`` (ERE) sub-pattern that matches ``identifier`` as a whole token (#548).

    A numeric comment ``databaseId`` (e.g. ``123``) embedded in a bare alternation
    is unanchored, so ``git log -E --grep '(123)'`` ALSO matches a commit that only
    addressed ``12345`` — ``123`` is a substring. In a PR carrying multiple outdated
    threads whose numeric ids overlap, the branch-evidence seed would then mark and
    resolve the WRONG thread. Anchor numeric ids with a non-digit boundary (or the
    message start/end) on both sides so ``123`` cannot match inside ``12345``. Node
    ids (``PRRT_…``) carry letters and ``_``, so a full-length token cannot collide
    by substring this way — they stay bare (only ``re.escape``-d).
    """
    escaped = re.escape(identifier)
    if identifier.isdigit():
        return f"(^|[^0-9]){escaped}([^0-9]|$)"
    return escaped


def _thread_has_blocking_comment_verdict(
    thread: ReviewThread,
    state: MonitorState,
) -> bool:
    """True if any of the thread's comment ids carries a non-resolvable verdict (#548).

    A single inline thread can accumulate per-comment verdicts under different
    comment databaseIds (the fix-cycle COMMENT path keys on ``comment_id``, so a
    thread with a reply addressed comment-by-comment can hold more than one). If
    ONE comment resolved (``fix_committed`` / ``false_positive``) but ANOTHER
    needs a human (``needs_human`` / ``agent_failed`` / ``defer``), the thread
    must stay open: promoting the resolvable verdict and resolving the thread
    would silently close — and let ``decide`` merge over — the blocking sibling.

    Returns ``True`` when any comment id (excluding the ``thread_id`` itself)
    carries a recorded verdict that is NOT in
    ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``. Both the in-memory reconcile and the
    branch-evidence seed consult this so neither path resolves a thread that still
    holds an unaddressed-by-a-human sibling verdict. Threads with no recorded
    comment verdicts (the common re-adoption case, where state is fresh) return
    ``False`` and behave exactly as before.
    """
    for comment_id in _thread_identifier_set(thread) - {thread.thread_id}:
        verdict = state.threads_addressed_ids.get(comment_id)
        if verdict is not None and verdict not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS:
            return True
    return False


def _parse_commit_iso(raw: str) -> datetime | None:
    """Parse a ``git log --format=%aI`` author date into a UTC-aware datetime.

    ``%aI`` is strict ISO 8601 with a numeric offset (never ``Z``, never naive),
    so a direct ``fromisoformat`` suffices; the result is normalized to UTC to
    compare cleanly against the UTC-aware review-comment timestamps. Returns
    ``None`` if git emitted something unparseable — the caller then falls back to
    the seed-anyway baseline rather than guessing post-fix ordering.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # pragma: no cover - %aI always carries an offset
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _addressed_comment_created_at(thread: ReviewThread, comment_id: str) -> datetime | None:
    """``created_at`` of the thread comment ``comment_id`` (its fix-time lower bound).

    The reconcile's post-fix activity guard (#548) compares the newest reviewer
    comment against the comment the fix actually addressed (the one carrying the
    resolvable verdict). The reviewer posted that comment no later than AWF fixed
    it, so its ``created_at`` is a conservative lower bound on fix time — any
    reviewer comment newer than it is feedback AWF never re-triaged.

    Use ``created_at`` ONLY, not ``max(created_at, updated_at)``: when the addressed
    comment is itself EDITED after the fix, its ``updated_at`` advances past the fix
    time and is simultaneously the newest reviewer activity that
    ``_latest_reviewer_comment_at`` reports. A ``updated_at`` baseline would then move
    in lockstep with that activity, so ``latest_comment_at > addressed_at`` could never
    fire and the edited body would be snapshotted as handled — silently closing an
    untriaged edit (#548 / PRRT_kwDOSJAM6s6JH9Zx). ``created_at`` cannot move with an
    edit, so it stays a true lower bound and the guard catches the edit. The cost is a
    conservative ``needs_human`` when a comment was edited BEFORE the fix (ordering is
    unprovable without a recorded fix time), which surfaces to a human rather than
    merging over feedback — the safe direction, matching the rest of this module.

    Returns ``None`` when the comment carries no ``created_at`` (the guard then keeps
    the promote-baseline, mirroring the branch-evidence seed when ordering is unprovable).
    """
    for comment in thread.comments:
        if comment.comment_id == comment_id:
            return comment.created_at
    return None


def _latest_reviewer_comment_at(thread: ReviewThread) -> datetime | None:
    """Newest non-viewer (reviewer/human/bot) comment timestamp on the thread.

    The forge clients already drop AWF's own (``viewer_did_author``) comments
    from the thread feed, but filter again here so the ordering check considers
    only reviewer activity even if a caller passes an unfiltered thread. Both
    ``created_at`` (a fresh reply) and ``updated_at`` (an edited comment — which
    also changes the resolution body hash) count as activity. Returns ``None``
    when no reviewer comment carries a usable timestamp.
    """
    latest: datetime | None = None
    for comment in thread.comments:
        if comment.viewer_did_author:
            continue
        for stamp in (comment.created_at, comment.updated_at):
            if stamp is not None and (latest is None or stamp > latest):
                latest = stamp
    return latest


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

    Post-fix reviewer activity guard: the body-hash snapshot is taken HERE, at
    re-adoption time, not at the prior instance's fix time. So if a reviewer replied
    to the outdated thread AFTER that fix but BEFORE this re-adoption, the reply is
    already baked into the seeded hash and ``_review_thread_needs_attention`` could
    never fire for it — seeding ``fix_committed`` blindly would resolve (and possibly
    merge) over that untriaged feedback. To prevent it, the seed compares the fix
    commit's author time against the newest non-viewer comment timestamp: when a
    reviewer comment provably postdates the matching fix commit, seed ``needs_human``
    instead of ``fix_committed``. ``needs_human`` is NOT in
    ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS`` (so the resolve loop leaves the thread
    open) and ``decide``'s outdated gate treats it as a merge blocker (``NotifyHuman``),
    so the fresh feedback surfaces to a human instead of being silently merged over —
    while the common case (no post-fix reply, or a comment that predates the fix) still
    seeds ``fix_committed`` and unblocks the PR. When the fix commit time or the comment
    timestamps are unavailable we cannot prove post-fix ordering, so we keep the
    seed-``fix_committed`` baseline (the alternative — never seeding — leaves the PR
    permanently BLOCKED on an invisible collapsed conversation). The guard still
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
        # #548: never seed ``fix_committed`` from branch evidence onto a thread
        # that already holds a blocking sibling comment verdict (``needs_human`` /
        # ``agent_failed`` / ``defer``). The matching ``fix: address`` commit for a
        # resolved sibling would otherwise re-introduce the merge-gate bypass the
        # reconcile guard above closes. A fresh re-adoption state carries no comment
        # verdicts, so this never changes the common branch-evidence path.
        and not _thread_has_blocking_comment_verdict(thread, state)
    ]
    if not unseeded:
        return
    worktree_path = self._worktrees_root / workspace_id

    async def _matching_fix_commit_time(thread: ReviewThread) -> tuple[bool, datetime | None]:
        # ``-E --all-match`` requires BOTH ``--grep`` patterns present in one commit
        # message: the literal ``fix: address`` prefix AND an alternation over the
        # thread's full identifier set (``thread_id`` plus any review-comment
        # databaseIds — #547). Multiple ``--grep`` with ``--all-match`` is AND, so
        # the ids cannot each be their own ``--grep`` (that would require ALL ids in
        # one commit); they are OR-ed inside a single ``-E`` alternation instead, so
        # a comment-path fix (``fix: address review comment issue:4688598838 — …``)
        # matches on the raw databaseId token even though ``thread_id`` differs.
        # Each id goes through ``_grep_id_pattern`` (numeric databaseIds get a
        # non-digit boundary so ``123`` cannot match inside ``12345`` — #548; node
        # ids stay ``re.escape``-d) and the set is sorted for a deterministic argv.
        # The literal ``fix: address``
        # carries no regex metacharacters so it is safe as its own pattern under
        # ``-E``. Still requiring ``fix: address`` keeps the match specific enough
        # not to seed a thread the branch never addressed. ``%aI`` returns the
        # newest matching commit's AUTHOR time so the caller can detect reviewer
        # replies that postdate the fix. Author (not committer, ``%cI``) time is
        # deliberate: AWF's rebase recovery (``control/executor/git_methods.py``)
        # runs ``git rebase`` on a stale PR branch, which rewrites every replayed
        # commit's COMMITTER date while preserving its author date. In the sequence
        # fix commit → reviewer follow-up → rebase recovery → re-adoption, the
        # rewritten committer date is newer than the follow-up, so a ``%cI`` ordering
        # would seed ``fix_committed`` over the untriaged reply; the author date stays
        # anchored to the original fix time and keeps the post-fix guard correct.
        # Returns ``(matched, commit_time)``: an empty / failed read means no durable
        # evidence (no seed); a non-empty read is a match whose time may still be
        # ``None`` if git emitted something unparseable.
        id_alternation = (
            "("
            + "|".join(_grep_id_pattern(i) for i in sorted(_thread_identifier_set(thread)))
            + ")"
        )
        result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "log",
                "-n",
                "1",
                "--format=%aI",
                "-E",
                "--all-match",
                "--grep",
                "fix: address",
                "--grep",
                id_alternation,
                "HEAD",
            )
        )
        raw = result.stdout.strip()
        if not (result.ok and raw):
            return False, None
        return True, _parse_commit_iso(raw)

    # The per-thread evidence greps are independent, read-only ``git log`` reads,
    # so run them concurrently (one re-adoption can carry several unseeded outdated
    # threads) and apply verdicts afterwards in deterministic thread order.
    evidence = await asyncio.gather(*(_matching_fix_commit_time(thread) for thread in unseeded))
    for thread, (matched, commit_time) in zip(unseeded, evidence, strict=True):
        if not matched:
            continue
        latest_comment_at = _latest_reviewer_comment_at(thread)
        if (
            commit_time is not None
            and latest_comment_at is not None
            and latest_comment_at > commit_time
        ):
            # A reviewer replied (or edited a comment) AFTER the matching fix commit
            # but before this re-adoption: that feedback is untriaged. Seed
            # ``needs_human`` (not ``fix_committed``) so the resolve loop leaves the
            # thread open and ``decide`` blocks merge on it (``NotifyHuman``) instead
            # of resolving/merging over the fresh feedback. Plain ``mark_addressed``
            # (no body-hash snapshot) suffices — the ``needs_human`` block is keyed on
            # the verdict alone, not the hash.
            state.mark_addressed(thread.thread_id, "needs_human")
            continue
        _mark_review_thread_addressed(state, thread, "fix_committed")


def _reconcile_comment_keyed_outdated_verdicts(
    status: PRStatus,
    state: MonitorState,
) -> None:
    """Promote a comment-keyed resolvable verdict onto its outdated thread (#547).

    The fix-cycle's COMMENT path records a verdict under the review comment's
    ``databaseId`` (``comment_id``), not the ``thread_id``. The same inline
    comment also surfaces as a ``ReviewThread`` whose ``comments`` carry that
    ``comment_id``. So an outdated thread can be fully addressed in memory yet
    invisible to the thread-keyed resolve loop (the #540 case: a continuous
    monitor whose comment-keyed verdict was in state, but the thread-keyed
    lookup missed it).

    Reconcile in memory BEFORE the branch-evidence seed: for each outdated thread
    with no ``thread_id`` verdict yet, if ANY of its comment ids carries a
    resolvable verdict (``_OUTDATED_RESOLVABLE_THREAD_VERDICTS`` — still excludes
    ``defer``), promote it onto the thread via ``_mark_review_thread_addressed``.
    That records the ``thread_id`` verdict AND the current thread body-hash
    snapshot, so the resolve loop's ``_review_thread_needs_attention`` guard sees
    a matching hash and resolves — mirroring how the branch-evidence seed
    promotes durable evidence. Because promotion sets the ``thread_id`` verdict,
    the seed step's ``unseeded`` filter then excludes these threads, so the pure
    in-memory case issues no git call.

    A bare verdict-membership broadening (without promotion) would still be
    blocked: ``_review_thread_needs_attention`` keys on the thread body-hash
    snapshot, which a comment-path verdict never stored, so the hash would
    mismatch and the resolve would be skipped. Promotion is what closes the gap.
    ``defer`` is excluded (a deferred thread must stay open with its tracking
    issue), and a thread the branch never addressed carries no resolvable verdict
    under any id, so it is not falsely promoted.

    A thread can also hold MIXED per-comment verdicts (one comment
    ``fix_committed``, a reply ``needs_human``). Promoting the resolvable verdict
    in that case would resolve the thread and let ``decide`` merge over the
    blocking sibling, so ``_thread_has_blocking_comment_verdict`` gates promotion:
    a thread with any non-resolvable comment verdict instead has ``needs_human``
    promoted onto its ``thread_id`` (#548 / PRRT_kwDOSJAM6s6JIGQB). Merely
    skipping is insufficient — ``decide``'s outdated gate consults only the
    ``thread_id`` verdict, never the comment ids, so a comment-keyed blocking
    verdict alone would not block the merge; promoting ``needs_human`` keeps the
    thread open AND makes ``decide`` return ``NotifyHuman``.

    Finally, an UNTRIAGED reviewer reply (a new comment carrying no verdict) that
    postdates the comment-path fix must not be silently resolved over. Because
    promotion snapshots the thread's CURRENT body via ``_mark_review_thread_addressed``,
    the reply would be baked into the snapshot and ``_review_thread_needs_attention``
    would see a matching hash and resolve — unlike the thread-keyed path, whose
    snapshot predates the reply and blocks. Mirroring the branch-evidence seed's
    post-fix activity guard, when a reviewer comment is newer than the addressed
    comment this seeds ``needs_human`` (resolve loop skips it, ``decide`` blocks
    merge) instead of promoting the resolvable verdict
    (#548 / PRRT_kwDOSJAM6s6JHeA2).
    """
    for thread in status.outdated_unresolved_inline_threads:
        if state.threads_addressed_ids.get(thread.thread_id) is not None:
            continue
        # #548: a thread can hold mixed per-comment verdicts — e.g. one comment
        # ``fix_committed`` and a reply ``needs_human``. Promoting the resolvable
        # verdict here (and resolving the thread) would silently bypass the
        # blocking sibling's safety gate.
        #
        # But a bare ``continue`` is NOT enough to keep ``decide`` blocking
        # (PRRT_kwDOSJAM6s6JIGQB): the blocking verdict is recorded under the
        # sibling's ``comment_id``, while ``decide``'s outdated gate
        # (``_outdated_thread_blocks_merge``) only consults ``thread.thread_id``,
        # the requeue flag, and the body-hash snapshot — it never looks up the
        # thread's comment ids. So skipping would leave the outdated thread with
        # NO thread-keyed verdict and the PR could auto-merge over the blocking
        # sibling. Promote a ``needs_human`` verdict onto the ``thread_id`` so the
        # gate's ``== "needs_human"`` check fires (``decide`` → ``NotifyHuman``).
        # Plain ``mark_addressed`` (no body-hash snapshot) suffices — ``needs_human``
        # is not in ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS`` so the resolve loop
        # still leaves the thread open, and the outdated gate keys on the verdict
        # alone, not the hash.
        if _thread_has_blocking_comment_verdict(thread, state):
            state.mark_addressed(thread.thread_id, "needs_human")
            continue
        comment_ids = sorted(_thread_identifier_set(thread) - {thread.thread_id})
        for comment_id in comment_ids:
            verdict = state.threads_addressed_ids.get(comment_id)
            if verdict not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS:
                continue
            # Post-fix reviewer activity guard (#548 / PRRT_kwDOSJAM6s6JHeA2),
            # mirroring the branch-evidence seed above. ``_mark_review_thread_addressed``
            # snapshots the thread's CURRENT body, so a reviewer reply that landed
            # AFTER the comment-path fix would be baked into the snapshot and
            # ``_review_thread_needs_attention`` would see a matching hash and resolve
            # over it — unlike the thread-keyed path, whose snapshot predates the reply
            # and blocks. The comment-path verdict stored no thread body-hash at fix
            # time, so detect post-fix activity by timestamp: when a reviewer comment
            # is newer than the addressed comment's CREATION, seed ``needs_human`` (which
            # the resolve loop skips and ``decide`` treats as a merge blocker) instead of
            # promoting the resolvable verdict. The baseline is the addressed comment's
            # ``created_at``, NOT ``max(created_at, updated_at)``: an EDIT to the addressed
            # comment itself advances its ``updated_at`` and is also the newest reviewer
            # activity, so a ``updated_at`` baseline would move in lockstep and never fire
            # (PRRT_kwDOSJAM6s6JH9Zx). When timestamps are unavailable we cannot prove
            # ordering, so keep the promote-baseline (the common no-activity case still
            # resolves and unblocks the PR).
            addressed_at = _addressed_comment_created_at(thread, comment_id)
            latest_comment_at = _latest_reviewer_comment_at(thread)
            if (
                addressed_at is not None
                and latest_comment_at is not None
                and latest_comment_at > addressed_at
            ):
                state.mark_addressed(thread.thread_id, "needs_human")
            else:
                _mark_review_thread_addressed(state, thread, verdict)
            break


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
    # #547: reconcile comment-keyed verdicts onto their outdated thread FIRST, so
    # the (continuous-monitor) in-memory case resolves without a git call and the
    # branch-evidence seed below naturally skips the now-seeded threads.
    _reconcile_comment_keyed_outdated_verdicts(status, state)
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
                state=state,
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
        # Also clear any stale bounded-retry count for this context so a recovered
        # blip never accumulates toward the budget across polls.
        await self._clear_forge_transient_retry_state_on_success(
            workspace_id=workspace_id,
            state=state,
            context="resolve_outdated_thread",
        )
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
