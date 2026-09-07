"""Allowlisted monitor-state carried across a PR re-adoption (issue #911).

When :class:`~awf.service.pr_monitor_adoption.PullRequestMonitorAdoptionService`
supersedes a *terminal* predecessor for the same repo/PR
(``PR_ADOPTION_SUPERSEDED_TERMINAL_WORKSPACE``) the successor used to start with
an empty ``monitor_threads_addressed`` and re-dispositioned every comment the
predecessor had already triaged (observed on aira-infra PR #229: successor
``ws_6fee851e74804257958b159b`` re-triaged ``5120013294``, ``issue:5549804922``
and ``issue:5549805025`` after ``ws_8742af8348794904b3ce5ac5`` had already marked
them ``false_positive``).

This module owns the pure, I/O-free policy for what may cross that boundary. It
is an **allowlist**, not a denylist: only comment/thread verdicts and the
evidence-marker classes that keep those verdicts (and the re-queues they imply)
honest are copied, so a marker class added later is dropped by default rather
than silently inherited.

The allowlist is additionally gated on *head continuity*: ``fix_committed`` and
``false_positive`` are both claims about the code at one particular head -- the fix
is in the branch, or the branch already refutes the reviewer -- and a force-push or
a revert before re-adoption can invalidate either, so they cross only when the
adopted head is the head the predecessor processed. The remaining verdicts judge
the feedback rather than the code and cross either way.

Deliberately never copied: protected-block state, awaiting-required-checks
timestamps, operator-hint *cycle* bookkeeping (the pending-hint record and its
processed markers -- as opposed to the per-thread operator decision below),
awaiting-workflow-scope / merge-block markers, notify/settle/grace bookkeeping,
and defer/needs-human reason text.
Those describe the *previous run's* position on a PR that has since moved; the
fresh monitor must re-derive them from the live PR.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

PR_ADOPTION_SEEDED_EVENT_TYPE = "workspace.pr_monitor_adoption_seeded"
PR_ADOPTION_SEEDED_REASON = "PR_ADOPTION_SEEDED_FROM_PREDECESSOR"
PR_ADOPTION_OPERATOR_HINT_REASON = "PR_ADOPTION_OPERATOR_HINT"

# The seedable verdicts that assert something about the *code at a head* rather
# than about the feedback: ``fix_committed`` means "the predecessor's fix is in the
# PR head", and ``false_positive`` means "the code at that head already refutes the
# reviewer" (AWF's own verdict guidance routes an already-satisfied comment to
# FALSE POSITIVE rather than FIXED, so the verdict rests on branch content too). A
# force-push or a revert between the predecessor's last poll and re-adoption can
# invalidate either while leaving the comment byte-identical, so the successor
# would suppress still-valid feedback -- and, since neither verdict re-enters
# ``AddressComments`` nor blocks the merge gate, auto-merge over it. Both are
# therefore inherited only when head continuity is established -- see
# :func:`head_continuity_established`.
_HEAD_DEPENDENT_VERDICTS = frozenset({"fix_committed", "false_positive"})

# Verdicts that judge the *feedback*, not the code, and so survive a head that
# moved: ``defer`` / ``needs_human`` are dispositions of the reviewer's ask (and
# both block the merge gate), and ``agent_failed`` re-queues either way.
_HEAD_INDEPENDENT_VERDICTS = frozenset(
    {
        "defer",
        "needs_human",
        # ``needs_comment_attention`` still re-queues ``agent_failed``, so seeding
        # it inherits the lineage without suppressing a retry.
        "agent_failed",
    }
)

# The comment-disposition vocabulary written by the monitor's feedback policy
# (``awf.runtime.feedback_policy`` / ``MonitorState.mark_addressed``). A bare-id
# key whose value falls outside it is some other bookkeeping entry and is not
# seeded -- the allowlist gates on the value as well as the key shape.
_SEEDABLE_VERDICTS = _HEAD_DEPENDENT_VERDICTS | _HEAD_INDEPENDENT_VERDICTS

# A verdict key is exactly one of the forge-neutral thread/comment id forms the
# adoption contract names: a GraphQL review-thread id (``PRRT_...``), a bare
# numeric review-comment id, an ``issue:<numeric-id>`` issue-comment id, or the
# Bitbucket encodings ``bb:<owner>/<repo>#<pr>:<id>`` /
# ``bbtask:<owner>/<repo>#<pr>:<id>`` / ``bbcomment:<numeric-id>`` (see
# ``awf.common.bitbucket_client_parsing``; top-level PR comments use
# ``bbcomment:`` via ``build_general_review_comments``). The pattern is closed
# rather than "any identifier-ish key", so a malformed, legacy, or future
# bookkeeping entry cannot cross the supersede boundary just by holding a
# verdict-shaped value. None of the alternatives can begin with ``_``, which
# keeps every ``__...__`` reserved marker structurally ineligible as well.
_VERDICT_KEY_RE = re.compile(
    r"^(?:"
    r"PRRT_[A-Za-z0-9_-]+"
    r"|[0-9]+"
    r"|issue:[0-9]+"
    r"|bbcomment:[0-9]+"
    r"|bb:[^/]+/[^#]+#\d+:\d+"
    r"|bbtask:[^/]+/[^#]+#\d+:\d+"
    r")$"
)

# Evidence markers that must travel with a copied verdict. Body hashes let the
# runner re-queue a comment/thread whose body changed since the predecessor
# triaged it (and, when present, keep an unchanged seeded verdict from being
# treated as stale on the successor's first poll). The deferred-issue marker
# prevents filing a duplicate follow-up issue.
#
# ``__operator_decision__:<thread id>`` (``awf.runtime.monitor_state_keys``) is
# the ruling that un-parked a ``needs_human`` thread: the guide *clears* the
# verdict (issue #938), so such a thread crosses this boundary as a body hash
# with no verdict and the successor re-queues it into ``AddressComments``.
# Without the ruling the repair prompt is rebuilt from the reviewer text alone
# and the agent can repeat the rejected approach and re-park -- exactly the loop
# issue #939 exists to break. It is copied regardless of head continuity: like
# ``defer``/``needs_human`` it disposes of the *feedback* rather than asserting
# what the branch contains, it never suppresses feedback nor unblocks the merge
# gate, and it reaches the agent only as quoted untrusted evidence in the repair
# prompt. It is redacted and length-capped where it is written, so no unbounded
# or secret-bearing text crosses. A thread that records any verdict other than
# ``agent_failed`` drops the marker at the source, so a marker that survives to
# adoption always belongs to a thread still owed an answer.
#
# ``__operator_decision_retired__:<thread id>`` is the same ruling *parked*
# because a verdict answered it, held so a rollback of that still-unconfirmed
# verdict restores it with the thread. It carries the same bounded, redacted text
# and the same "quoted evidence only" reach, so it crosses on the same terms --
# and adoption dropping a verdict is itself such a rollback, so
# :func:`_restore_orphaned_operator_decisions` un-parks it when its verdict does
# not cross *and* head continuity holds. Unlike the live marker, un-parking is
# gated on continuity: this ruling was already consumed once and the verdict it
# produced is exactly the head-dependent verdict the boundary just discarded, so
# replaying it as a live directive ("follow that ruling, do not escalate") on a
# head that moved would re-manufacture the discarded verdict against code that
# may no longer support it -- and neither ``fix_committed`` nor ``false_positive``
# blocks the merge gate (PRRT_kwDOSJAM6s6fyCVP). Without continuity the parked
# ruling is dropped rather than left in place, so a later rollback on the
# successor cannot revive it against the new head either.
#
# ``__operator_decision_at__:<thread id>`` is the issue-time binding for a ruling
# that crossed with no body hash (the hygiene-seeded ``needs_human`` rows a guide
# clears): it is what lets the successor retire the ruling when a reviewer replied
# after the operator read the thread. It travels with the ruling for the same
# reason body hashes travel with verdicts -- left behind, the successor would quote
# guidance the reviewer's reply already outran (PRRT_kwDOSJAM6s6fxBwT). The value is
# an ISO-8601 timestamp AWF wrote itself, so nothing untrusted crosses with it.
_OPERATOR_DECISION_PREFIX = "__operator_decision__:"
_OPERATOR_DECISION_ISSUED_AT_PREFIX = "__operator_decision_at__:"
_RETIRED_OPERATOR_DECISION_PREFIX = "__operator_decision_retired__:"
_COPIED_MARKER_PREFIXES = (
    "__review_comment_body_hash__:",
    "__review_thread_body_hash__:",
    "__deferred_issue_filed__:",
    _OPERATOR_DECISION_PREFIX,
    _OPERATOR_DECISION_ISSUED_AT_PREFIX,
    _RETIRED_OPERATOR_DECISION_PREFIX,
)

# A head SHA counts as continuity evidence only in its full 40-hex form. An
# abbreviation is a prefix, not a commit identity: two equal abbreviations can
# name different commits, so comparing them would establish continuity without
# proving it. A value that is whitespace-only, empty after stripping, or
# otherwise non-hex is not a commit identifier at all -- and two such values
# would compare equal to each other. Both read as "not established".
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def head_continuity_established(
    *,
    adopted_head_sha: str | None,
    predecessor_head_sha: str | None,
) -> bool:
    """True when the adopted PR head is provably the head the predecessor processed.

    Adoption has no git or ancestry oracle in this transaction, so continuity is
    only *established* by SHA equality. Any moved head -- force-push, revert, or
    plain new commits on top -- reads as "not established", which is deliberately
    conservative: the cost is that the successor re-triages the code-dependent
    items, while the alternative is inheriting a fix -- or a refutation -- that may
    no longer exist in the branch and merging over the reviewer's still-open
    feedback.

    Both sides are normalized (surrounding whitespace and case are forge noise,
    not a discontinuity) *before* they are validated, and only a full 40-hex
    commit SHA is accepted as evidence. An abbreviated, blank, whitespace-only,
    or otherwise non-hex value on either side is not proof of identity and fails
    closed -- equality between two such values must not establish continuity.
    """
    adopted = (adopted_head_sha or "").strip().lower()
    predecessor = (predecessor_head_sha or "").strip().lower()
    if _FULL_COMMIT_SHA_RE.match(adopted) is None:
        return False
    if _FULL_COMMIT_SHA_RE.match(predecessor) is None:
        return False
    return adopted == predecessor


def seedable_monitor_state(
    previous: Mapping[str, Any] | None,
    *,
    head_continuity: bool = False,
) -> dict[str, str]:
    """Return the allowlisted subset of a predecessor's monitor state.

    ``head_continuity`` states whether the head the successor adopts still carries
    the predecessor's processed head (:func:`head_continuity_established`). It
    fails closed: without it, the head-dependent ``fix_committed`` /
    ``false_positive`` verdicts are dropped and re-triaged -- along with any
    parked operator ruling they answered, which would otherwise be replayed as a
    live directive and recreate the dropped verdict -- while the head-independent
    dispositions still cross.

    The result is key-sorted (deterministic ``copied_keys`` in the seeded event)
    and is always a fresh dict, so callers may mutate it -- e.g. to arm a pending
    operator hint -- without touching the predecessor's persisted state.
    """
    if not previous:
        return {}
    seeded = {
        key: value
        for key, value in previous.items()
        if isinstance(value, str)
        and (
            _is_verdict_entry(key, value, head_continuity=head_continuity)
            or _is_copied_marker(key, value)
        )
    }
    return dict(
        sorted(
            _restore_orphaned_operator_decisions(seeded, head_continuity=head_continuity).items()
        )
    )


def _restore_orphaned_operator_decisions(
    seeded: dict[str, str], *, head_continuity: bool
) -> dict[str, str]:
    """Un-park a ruling whose answering verdict did not cross the boundary.

    ``_mark_review_thread_addressed`` parks the operator ruling under
    ``__operator_decision_retired__:<id>`` when a verdict answers it, and
    ``_clear_addressed_state_by_id`` un-parks it whenever that still-unconfirmed
    verdict is rolled back. Dropping a verdict here is the same rollback: the
    successor re-queues the unchanged thread into ``AddressComments``, so it must
    carry the ruling or the agent re-reads only the reviewer text it already
    escalated on and can repeat the rejected approach and re-park (issue #939).

    A ruling therefore stays parked only alongside the verdict it answered. With
    that verdict gone it is promoted back to a live decision -- unless the
    operator has since issued a fresh one, which is their latest word on the
    thread and supersedes the parked copy.

    That promotion requires head continuity. A ruling reaches this sidecar only
    by having already produced a verdict, and on a moved head the verdict it
    produced is the head-dependent one this boundary just dropped as no longer
    provably true of the branch. Quoting the ruling as a live directive -- the
    repair prompt tells the agent to follow it and not to re-escalate -- would
    recreate that verdict against code that may have been force-pushed away, and
    ``fix_committed`` / ``false_positive`` neither re-enter ``AddressComments``
    nor block the merge gate, so the PR could merge over still-valid feedback
    (PRRT_kwDOSJAM6s6fyCVP). Without continuity the ruling is therefore dropped
    outright rather than left parked: a parked copy would otherwise be revived
    against the new head by the successor's own next verdict rollback. The
    successor re-triages the thread from the reviewer text alone and escalates
    again if it must -- the same conservative re-triage the dropped verdict buys.
    """
    restored = dict(seeded)
    for key, value in seeded.items():
        if not key.startswith(_RETIRED_OPERATOR_DECISION_PREFIX):
            continue
        item_id = key[len(_RETIRED_OPERATOR_DECISION_PREFIX) :]
        if item_id in seeded:
            continue
        del restored[key]
        if not head_continuity:
            continue
        live_key = f"{_OPERATOR_DECISION_PREFIX}{item_id}"
        if live_key not in seeded:
            restored[live_key] = value
    return restored


def _is_verdict_entry(key: str, value: str, *, head_continuity: bool) -> bool:
    seedable = _SEEDABLE_VERDICTS if head_continuity else _HEAD_INDEPENDENT_VERDICTS
    return value in seedable and _VERDICT_KEY_RE.match(key) is not None


def _is_copied_marker(key: str, value: str) -> bool:
    if not value:
        return False
    return any(
        key.startswith(prefix) and len(key) > len(prefix) for prefix in _COPIED_MARKER_PREFIXES
    )
