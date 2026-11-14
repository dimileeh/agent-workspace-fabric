"""Operator ruling that closes an ``AGENT_NON_FIXED_WITH_MUTATION`` park (issue #1213).

It also settles a resolved thread the READY_TO_MERGE disposition gate flagged
``REVIEW_THREAD_DISPOSITION_UNVERIFIED`` (#1238), e.g. one whose opening bot
kept replying after AWF's marker until the auto-reopen budget ran out.

A correction attempt that mutated the worktree and then reported a non-FIXED
verdict is rolled back and its review thread parked ``needs_human``. When the
operator answers with a guide such as "PRRT_x FIXED by <sha>; make no code
changes", the agent cycle had nothing to do: the hint was consumed and
``decide()`` returned to the identical ``NotifyHuman`` on every poll. No path
applied an operator ruling as a disposition without a worktree mutation.

This module applies such a ruling without the agent, the same way #1189's
``operator_preserved_attempt`` applies its ruling. A ruling is one guide line in
a strict, machine-readable grammar (#1215), never English prose:

    AWF-RULING <thread_id> FIXED <sha>
    AWF-RULING <thread_id> FALSE_POSITIVE[: <reason>]

The whole guide is checked before anything changes. Every ruled thread must be
a live mutation park, open on the PR, with the conversation the park answered
unchanged; or a gate-flagged thread that is not listed open and that a
single-thread audit shows resolved by AWF's own identity. A FIXED claim needs
evidence: the cited commit must resolve and be an ancestor of the PR head. A thread the same ruling already resolved is
satisfied, so a repeated guide retries only the threads still parked. Each
thread then records the operator-sourced
disposition, gets AWF's disposition reply, and is resolved. Each step is
audited under its reason code.

Anything short of that fails closed: no thread changes, and the hint parks
``needs_human`` with ``OPERATOR_PARK_DISPOSITION_UNAPPLIED`` and a detail. A
guide that carries no ruling line, or rules on no mutation park or gate-flagged
thread, never reaches any of this, so the agent cycle (and the #938 clear-and-retriage path) runs as
before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationType
from awf.runtime.feedback_policy import (
    canonical_unresolved_inline_threads,
    mutation_park_record,
    recorded_review_thread_body_matches,
    review_thread_body_hash,
    review_thread_body_state_key,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _disposition_unverified_key, _mutation_park_key
from awf.runtime.operator_hints import mark_operator_hint_needs_human
from awf.runtime.pr_monitor import (
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner.helpers import _agent_failed_reason_state_key
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from awf.runtime.pr_monitor_runner.operator_hint_parsing import (
    _OPERATOR_HINT_REVIEW_THREAD_ID_RE,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.operator_preserved_attempt import PreservedAttemptOutcome
from awf.runtime.pr_monitor_runner.thread_disposition import (
    REVIEW_THREAD_AUDIT_UNAVAILABLE,
    REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    THREAD_DISPOSITION_REPLY_FAILED,
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
    retire_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_reply import (
    persist_parked_thread,
    record_resolve_landed,
    reply_before_resolve,
)

OPERATOR_PARK_DISPOSITION_APPLIED = "OPERATOR_PARK_DISPOSITION_APPLIED"
OPERATOR_PARK_DISPOSITION_UNAPPLIED = "OPERATOR_PARK_DISPOSITION_UNAPPLIED"
OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED = "OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED"
_AUDIT_EVENT = "workspace.audit.operator_disposition"
_AUDIT_ACTION = "apply_park_disposition"
_DISPOSITION_SOURCE = "operator_disposition"
_RESOLVE_FAILED_REASON_PREFIX = f"{OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED}:"
# The forge faults ``reply_before_resolve`` parks a ruled thread under: the reply
# never posted, or its post-reply re-read failed. Either way a repeat may retry it.
_REPLY_FAULT_REASON_PREFIXES = (
    f"{THREAD_DISPOSITION_REPLY_FAILED}:",
    f"{REVIEW_THREAD_AUDIT_UNAVAILABLE}:",
)

# One ruling per line, the whole line and nothing else (#1215):
#   AWF-RULING <thread_id> FIXED <sha>
#   AWF-RULING <thread_id> FALSE_POSITIVE[: <reason>]
# Keywords are case-sensitive and the SHA is 7-40 hex digits. Any other line,
# however close, is not a ruling: prose is never parsed for one.
_RULING_LINE_RE = re.compile(
    r"AWF-RULING[ \t]+(?P<thread_id>\S+)[ \t]+"
    r"(?:FIXED[ \t]+(?P<sha>[0-9a-fA-F]{7,40})|FALSE_POSITIVE(?::[ \t]*\S.*)?)"
)

# ``git merge-base --is-ancestor`` answers "no", and ``git rev-parse --verify
# --quiet`` answers "no such commit", with exit 1. Any other non-zero exit is no
# answer at all (a command-runner timeout is 124, a git fatal 128).
_GIT_NO_RETURN_CODE = 1
# Bounds the pre-probe PR-branch fetch: a stalled remote times out into the
# same fall-through as a failed fetch instead of wedging the ruling.
_PR_BRANCH_FETCH_TIMEOUT_SECONDS = 60.0

_UNAPPLIED_EXPLANATION: dict[str, str] = {
    "thread_without_disposition": (
        "a thread has AWF-RULING lines that disagree, so AWF cannot tell what the guide "
        "rules for it."
    ),
    "not_mutation_park": (
        "a named thread is not parked on a refused correction attempt "
        "(AGENT_NON_FIXED_WITH_MUTATION)."
    ),
    "thread_not_open": "a named thread is not open on the PR any more.",
    "conversation_changed": (
        "a named thread's conversation changed since it was parked, so the ruling may not "
        "cover the newest feedback."
    ),
    "no_body_snapshot": (
        "AWF has no snapshot of the conversation the park answered, so it cannot prove the "
        "ruling covers it."
    ),
    "sha_unknown": "the cited commit does not resolve to a commit in the workspace.",
    "sha_not_ancestor": "the cited commit is not an ancestor of the PR head.",
    "ancestry_unverified": (
        "AWF could not verify that the cited commit is an ancestor of the PR head."
    ),
    "flagged_thread_open": (
        "a named gate-flagged thread is open on the PR again, so AWF re-triages it instead."
    ),
    "flagged_thread_not_resolved_by_awf": (
        "a named gate-flagged thread is no longer resolved by AWF's own identity."
    ),
    "flagged_thread_unreadable": "AWF could not re-read a named gate-flagged thread.",
}


@dataclass(frozen=True)
class ParkRuling:
    """One operator ruling on a parked thread."""

    kind: Literal["fixed", "false_positive"]
    sha: str | None = None


@dataclass(frozen=True)
class _Staged:
    thread_id: str
    thread: ReviewThread | None  # None: a gate-flagged thread, already resolved
    ruling: ParkRuling
    commit: str | None
    # A gate-flagged thread's outside comments as its audit read them; the
    # post-reply re-read refuses the ruling when they changed before the reply.
    audited_comments: tuple[str, ...] = ()


def park_dispositions(text: str | None) -> dict[str, ParkRuling | None] | None:
    """Per-thread ``AWF-RULING`` lines; ``None`` when the guide carries none.

    Only a line that full-matches the grammar, naming a review-thread id, is a
    ruling. A thread whose ruling lines disagree maps to ``None`` so the guide
    fails closed rather than guessing. The optional false-positive reason is the
    operator's note: it stays with the guide and is never posted.
    """
    found: dict[str, set[ParkRuling]] = {}
    for line in (text or "").splitlines():
        match = _RULING_LINE_RE.fullmatch(line.strip())
        if match is None or not _OPERATOR_HINT_REVIEW_THREAD_ID_RE.fullmatch(
            match.group("thread_id")
        ):
            continue
        sha = match.group("sha")
        ruling = ParkRuling("fixed", sha.lower()) if sha else ParkRuling("false_positive")
        found.setdefault(match.group("thread_id"), set()).add(ruling)
    if not found:
        return None
    return {
        thread_id: next(iter(rulings)) if len(rulings) == 1 else None
        for thread_id, rulings in found.items()
    }


def _is_ruleable_park(state: MonitorState, thread_id: str) -> bool:
    """A live mutation park, or one an operator ruling re-parked on a forge fault.

    The ruling drops the mutation-park record before its reply, so a refused reply
    or a failed resolve leaves the thread parked under another code; a repeated
    guide retries it instead of being refused as ``not_mutation_park``.
    """
    if mutation_park_record(state.threads_addressed_ids, thread_id) is not None:
        return True
    if state.threads_addressed_ids.get(thread_id) != "needs_human":
        return False
    reason = state.threads_addressed_ids.get(_needs_human_reason_state_key(thread_id)) or ""
    if reason.startswith(_RESOLVE_FAILED_REASON_PREFIX):
        return True
    disposition = read_item_disposition(state, thread_id)
    return (
        disposition is not None
        and disposition.source == _DISPOSITION_SOURCE
        and reason.startswith(_REPLY_FAULT_REASON_PREFIXES)
    )


def _is_gate_flagged(state: MonitorState, thread_id: str) -> bool:
    """The READY_TO_MERGE disposition gate flagged this AWF-resolved thread (#1238)."""
    return (
        state.threads_addressed_ids.get(_disposition_unverified_key(thread_id))
        == REVIEW_THREAD_DISPOSITION_UNVERIFIED
    )


def _listed_open(status: PRStatus, thread_id: str) -> bool:
    return any(
        thread.thread_id == thread_id
        for thread in (
            *status.unresolved_inline_threads,
            *status.outdated_unresolved_inline_threads,
        )
    )


async def _flagged_thread_problem(
    self: Any, *, repo: RepoRef, pr_number: int, thread_id: str
) -> tuple[tuple[str, ...], str | None]:
    """``(outside comments, None)`` for a gate-flagged thread resolved by AWF, else ``((), why)``.

    The outside comments are the conversation the ruling covers, as this audit
    read it: the post-reply re-read compares them (PRRT_kwDOSJAM6s63XfSQ).
    """
    try:
        audit = await self._deps.gh.fetch_review_thread_audit(
            repo=repo, pr_number=pr_number, thread_id=thread_id
        )
    except ForgeClientError as exc:
        _log.warning(
            "monitor.operator_park_disposition_flagged_thread_unreadable",
            thread_id=thread_id,
            forge_reason_code=exc.reason_code,
        )
        return (), "flagged_thread_unreadable"
    viewer = (audit.viewer_login or "").strip().casefold()
    live = next((t for t in audit.threads if t.thread_id == thread_id), None)
    if not audit.supported or not viewer or live is None:
        return (), "flagged_thread_unreadable"
    if not live.is_resolved or (live.resolved_by or "").strip().casefold() != viewer:
        return (), "flagged_thread_not_resolved_by_awf"
    return live.external_comment_bodies, None


def _ruling_already_applied(
    state: MonitorState, status: PRStatus, thread_id: str, ruling: ParkRuling
) -> bool:
    """This same ruling already replied to and resolved the thread, and it stays resolved.

    A guide whose other thread failed to resolve is retried by sending it again
    (PRRT_kwDOSJAM6s62f9ot): the thread it did close is satisfied, not refused as
    ``not_mutation_park``. A different ruling, or a reopened thread, is not.
    """
    disposition = read_item_disposition(state, thread_id)
    if disposition is None or disposition.source != _DISPOSITION_SOURCE:
        return False
    if not disposition.resolved or _listed_open(status, thread_id):
        return False
    if ruling.sha is None:
        return disposition.disposition_class == "false_positive"
    return disposition.disposition_class == "fixed" and any(
        commit.startswith(ruling.sha) for commit in disposition.commits
    )


def _live_thread(
    state: MonitorState, status: PRStatus, thread_id: str
) -> tuple[ReviewThread | None, str | None]:
    """The open thread (active or outdated) the ruling closes, or why it cannot."""
    thread = next(
        (
            candidate
            for candidate in canonical_unresolved_inline_threads(
                status.unresolved_inline_threads,
                status.outdated_unresolved_inline_threads,
                state.threads_addressed_ids,
            )
            if candidate.thread_id == thread_id
        ),
        None,
    )
    if thread is None:
        return None, "thread_not_open"
    recorded = state.threads_addressed_ids.get(review_thread_body_state_key(thread_id))
    if not recorded:
        return None, "no_body_snapshot"
    if not recorded_review_thread_body_matches(recorded, thread):
        return None, "conversation_changed"
    return thread, None


async def _ancestor_commit(
    self: Any,
    *,
    workspace_id: str,
    sha: str,
    head_sha: str,
    remote_branch: str | None,
    remote_push_url: str | None,
) -> tuple[str | None, str | None]:
    """``(full_sha, None)`` when ``sha`` is an ancestor of ``head_sha``, else ``(None, detail)``.

    Fails closed, unlike the #934 reachability probe: here the probe *is* the
    evidence, so anything short of a clean "yes" refuses the ruling. The PR
    branch is fetched first: after a mutation park the worktree sits on the
    rollback commit, so the PR head and the cited fix may exist only remotely.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    if not head_sha:
        return None, "ancestry_unverified"
    worktree_path = self._worktrees_root / workspace_id
    try:
        if remote_branch:
            fetched = await self._remote_branch_fetch_once(
                worktree_path=worktree_path,
                remote=remote_push_url or "origin",
                remote_branch=remote_branch,
                timeout_seconds=_PR_BRANCH_FETCH_TIMEOUT_SECONDS,
            )
            if not fetched.ok:
                # Objects already local may still prove the claim; the probe
                # below fails closed when they do not.
                _log.warning(
                    "monitor.operator_park_disposition_fetch_failed",
                    workspace_id=workspace_id,
                    remote_branch=remote_branch,
                    returncode=fetched.returncode,
                    reason_code=fetched.reason_code,
                )
        resolved = await self._deps.runner.run(
            git_worktree_command(
                worktree_path, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
        if not resolved.ok:
            if resolved.returncode == _GIT_NO_RETURN_CODE:
                return None, "sha_unknown"
            return None, "ancestry_unverified"
        full_sha = resolved.stdout.strip().lower()
        if not full_sha:
            return None, "ancestry_unverified"
        ancestry = await self._deps.runner.run(
            git_worktree_command(worktree_path, "merge-base", "--is-ancestor", full_sha, head_sha),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as probe_exc:
        _log.warning(
            "monitor.operator_park_disposition_ancestry_probe_failed",
            workspace_id=workspace_id,
            sha=sha,
            head_sha=head_sha,
            exc_type=type(probe_exc).__name__,
        )
        return None, "ancestry_unverified"
    if ancestry.ok:
        return full_sha, None
    if ancestry.returncode == _GIT_NO_RETURN_CODE:
        return None, "sha_not_ancestor"
    return None, "ancestry_unverified"


async def _stage(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    rulings: dict[str, ParkRuling | None],
    remote_branch: str | None,
    remote_push_url: str | None,
) -> tuple[list[_Staged], str | None]:
    """Check every named thread before any changes; ``(staged, None)`` or ``([], detail)``."""
    staged: list[_Staged] = []
    for thread_id, ruling in rulings.items():
        if ruling is None:
            return [], "thread_without_disposition"
        if _ruling_already_applied(state, status, thread_id, ruling):
            continue
        flagged = _is_gate_flagged(state, thread_id)
        thread: ReviewThread | None = None
        audited: tuple[str, ...] = ()
        if flagged and not _listed_open(status, thread_id):
            # #1238: resolved by AWF and flagged by the gate; the ruling is its disposition.
            audited, detail = await _flagged_thread_problem(
                self, repo=repo, pr_number=pr_number, thread_id=thread_id
            )
            if detail is not None:
                return [], detail
        elif _is_ruleable_park(state, thread_id):
            thread, detail = _live_thread(state, status, thread_id)
            if thread is None:
                return [], detail
        else:
            # An open flagged thread is ``decide()``'s to re-triage.
            return [], "flagged_thread_open" if flagged else "not_mutation_park"
        commit: str | None = None
        if ruling.sha is not None:
            commit, detail = await _ancestor_commit(
                self,
                workspace_id=workspace_id,
                sha=ruling.sha,
                head_sha=status.head_sha,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
            )
            if commit is None:
                return [], detail
        staged.append(_Staged(thread_id, thread, ruling, commit, audited))
    return staged, None


def _record_disposition(state: MonitorState, item: _Staged) -> str | None:
    """Record the operator's disposition on the thread; return the park's rollback head."""
    thread_id = item.thread_id
    record = mutation_park_record(state.threads_addressed_ids, thread_id)
    # A gate-flagged thread is resolved, so the forge lists no body to hash: it
    # keeps the one its last triage recorded, which a later reopen compares.
    body_hash = (
        review_thread_body_hash(item.thread)
        if item.thread is not None
        else state.threads_addressed_ids.get(review_thread_body_state_key(thread_id))
    )
    for key in (
        _needs_human_reason_state_key(thread_id),
        _mutation_park_key(thread_id),
        _agent_failed_reason_state_key(thread_id),
    ):
        state.threads_addressed_ids.pop(key, None)
    # The reply cites the commit, never the operator's free text: guide notes are
    # private to the operator and the thread is public.
    verdict = "fix_committed" if item.commit is not None else "false_positive"
    if item.thread is not None:
        _mark_review_thread_addressed(state, item.thread, verdict)
    else:
        state.mark_addressed(thread_id, verdict)
    if item.commit is not None:
        disposition = ThreadDisposition(
            item_id=thread_id,
            disposition_class="fixed",
            verdict="fix_committed",
            reason=f"Fixed by {item.commit[:12]} per operator ruling.",
            commits=(item.commit,),
            source=_DISPOSITION_SOURCE,
            thread_body_hash=body_hash,
        )
    else:
        disposition = ThreadDisposition(
            item_id=thread_id,
            disposition_class="false_positive",
            verdict="false_positive",
            reason="False positive per operator ruling.",
            source=_DISPOSITION_SOURCE,
            thread_body_hash=body_hash,
        )
    rollback_head = (record[0] or None) if record else None
    previous = read_item_disposition(state, thread_id)
    if previous is not None and previous.reply_comment_id:
        if (
            not previous.answers_resolution
            and previous.source == disposition.source
            and previous.reason == disposition.reason
            and previous.commits == disposition.commits
            and previous.thread_body_hash == disposition.thread_body_hash
        ):
            # This ruling's own reply landed but its resolve did not: the retry
            # resolves on that reply instead of posting it again.
            return rollback_head
        # A reply AWF posted for an earlier verdict answers nothing the operator
        # ruled on; retire it so its marker never certifies this resolution.
        retire_item_disposition(state, previous)
    record_item_disposition(state, disposition)
    return rollback_head


async def apply_park_disposition(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    hint: OperatorHint,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    monitor_log: WorkspaceLogSink | None,
    remote_push_url: str | None = None,
) -> PreservedAttemptOutcome | None:
    """Apply an operator ruling on mutation-parked threads before the hint's agent cycle.

    Returns ``None`` (the agent cycle runs as before) when the directive carries
    no ruling, when none of its rulings names a mutation park or a gate-flagged
    thread (an ordinary thread's ruling is the agent's to act on), or when a
    protected-scope block is pending: that resume is owned by the grant/directive protocol. Otherwise the hint is finalized (each
    thread replied to and resolved, or re-parked under its own reason code) or
    parked ``needs_human`` with the UNAPPLIED reason code, and the caller
    persists state and skips the agent.
    """
    rulings = park_dispositions(hint.directive)
    if rulings is None or state.has_preserved_protected_block:
        return None
    # A ruling line is deliberate, so one naming a mutation park or a gate-flagged
    # thread takes over the guide even when it conflicts: that fails closed below,
    # never to the agent. So does one this same ruling already settled, whose
    # flag the gate has since cleared: a repeated guide is a no-op.
    if not any(
        _is_ruleable_park(state, thread_id)
        or _is_gate_flagged(state, thread_id)
        or (ruling is not None and _ruling_already_applied(state, status, thread_id, ruling))
        for thread_id, ruling in rulings.items()
    ):
        return None
    staged, detail = await _stage(
        self,
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        status=status,
        state=state,
        rulings=rulings,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    audit: dict[str, Any] = {
        "workspace_id": workspace_id,
        "pr_number": pr_number,
        "status": status,
        "base_branch": base_branch,
        "remote_branch": remote_branch,
        "operation_id": operation_id,
        "monitor_log": monitor_log,
    }
    if detail is not None:
        await _record(
            self,
            **audit,
            outcome="unapplied",
            reason_code=OPERATOR_PARK_DISPOSITION_UNAPPLIED,
            evidence={
                "detail": detail,
                "thread_ids": list(rulings),
                "shas": [r.sha for r in rulings.values() if r is not None and r.sha],
                "head_sha": status.head_sha,
                "hint_operation_id": hint.operation_id,
            },
        )
        mark_operator_hint_needs_human(
            state,
            f"{OPERATOR_PARK_DISPOSITION_UNAPPLIED}: the operator ruling on a parked or "
            f"gate-flagged review thread was not applied ({detail}): "
            f"{_UNAPPLIED_EXPLANATION[detail]} Send a guide with one line per such thread: 'AWF-RULING <thread_id> FIXED <sha>' or "
            "'AWF-RULING <thread_id> FALSE_POSITIVE[: <reason>]'.",
        )
        return PreservedAttemptOutcome(
            outcome="operator_hint_park_disposition_unapplied",
            reason_code=OPERATOR_PARK_DISPOSITION_UNAPPLIED,
        )
    all_resolved = True
    for item in staged:
        rollback_head = _record_disposition(state, item)
        await _record(
            self,
            **audit,
            outcome="applied",
            reason_code=OPERATOR_PARK_DISPOSITION_APPLIED,
            evidence={
                "thread_id": item.thread_id,
                "disposition": item.ruling.kind,
                "sha": item.commit,
                "head_sha": status.head_sha,
                "rollback_head": rollback_head,
                "hint_operation_id": hint.operation_id,
            },
        )
        if not await _reply_and_resolve(self, repo=repo, state=state, item=item, **audit):
            all_resolved = False
    # No agent acted on the directive, so nothing else it names was answered: an
    # empty acted text keeps the answered-feedback sweep from dismissing a parked
    # comment the guide only mentions.
    _finalize_processed_operator_hint(state, hint=hint, acted_feedback_text="")
    if all_resolved:
        return PreservedAttemptOutcome(
            outcome="operator_hint_park_disposition_applied",
            reason_code=OPERATOR_PARK_DISPOSITION_APPLIED,
        )
    return PreservedAttemptOutcome(
        outcome="operator_hint_park_disposition_resolve_failed",
        reason_code=OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED,
    )


async def _reply_and_resolve(
    self: Any,
    *,
    repo: RepoRef,
    state: MonitorState,
    item: _Staged,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> bool:
    """Post the disposition reply, then resolve; ``False`` when the thread stays open.

    The same reply-before-resolve shape as the fix cycle: no later poll resolves
    an active thread outside a fix cycle, so leaving it unresolved would strand
    it (#305/#1018). A refused reply has already parked the thread under its own
    ``THREAD_DISPOSITION_*`` code. A failed resolve re-parks it ``needs_human``
    under ``OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED`` with the forge's reason
    code; the posted reply is kept, so a repeated guide reuses it. A gate-flagged
    thread is already resolved: the resolve is a no-op that records it landed.
    """
    thread_id = item.thread_id
    posted = await reply_before_resolve(
        self,
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_id=thread_id,
        state=state,
        action="resolve_thread",
        base_branch=base_branch,
        remote_branch=remote_branch,
        thread=item.thread,
        audited_comments=item.audited_comments,
        operation_id=operation_id,
        operation_type=OperationType.comment_repair.value,
        monitor_log=monitor_log,
        source_head_sha=status.head_sha,
        repo=repo,
    )
    if posted is None:
        await persist_parked_thread(
            self, workspace_id=workspace_id, state=state, thread_id=thread_id
        )
        return False
    try:
        await self._deps.gh.resolve_thread(thread_id=thread_id)
    except ForgeClientError as exc:
        state.mark_addressed(thread_id, "needs_human")
        state.mark_addressed(
            _needs_human_reason_state_key(thread_id),
            f"{_RESOLVE_FAILED_REASON_PREFIX} AWF posted the operator's disposition reply but "
            f"could not resolve the thread ({exc.reason_code}). Resolve it on the forge, or "
            "send the same guide again.",
        )
        await _record(
            self,
            workspace_id=workspace_id,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            monitor_log=monitor_log,
            outcome="needs_human",
            reason_code=OPERATOR_PARK_DISPOSITION_RESOLVE_FAILED,
            evidence={
                "thread_id": thread_id,
                "forge_reason_code": exc.reason_code,
                "disposition": posted.as_payload(),
            },
        )
        return False
    await record_resolve_landed(self, workspace_id=workspace_id, state=state, disposition=posted)
    return True


async def _record(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    monitor_log: WorkspaceLogSink | None,
    outcome: str,
    reason_code: str,
    evidence: dict[str, object],
) -> None:
    """Log and audit one disposition outcome under its reason code."""
    _log.info(
        "monitor.operator_park_disposition",
        workspace_id=workspace_id,
        pr_number=pr_number,
        outcome=outcome,
        reason_code=reason_code,
        **evidence,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_EVENT,
        action=_AUDIT_ACTION,
        outcome=outcome,
        reason_code=reason_code,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        monitor_log=monitor_log,
        evidence=evidence,
    )
