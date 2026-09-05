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
is an **allowlist**, not a denylist: only comment/thread verdicts and the two
evidence-marker classes that keep those verdicts honest are copied, so a marker
class added later is dropped by default rather than silently inherited.

The allowlist is additionally gated on *head continuity*: ``fix_committed`` claims
the fix is in the branch, which stops being true if the PR was force-pushed or the
fix reverted before re-adoption, so it crosses only when the adopted head is the
head the predecessor processed. The remaining verdicts judge the comment rather
than the branch and cross either way.

Deliberately never copied: protected-block state, awaiting-required-checks
timestamps, operator-hint bookkeeping, awaiting-workflow-scope / merge-block
markers, notify/settle/grace bookkeeping, and defer/needs-human reason text.
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

# ``fix_committed`` is the one seedable verdict that asserts something about the
# *branch* rather than about the comment: it means "the predecessor's fix is in
# the PR head". A force-push or a revert between the predecessor's last poll and
# re-adoption can drop that fix while leaving the comment byte-identical, so the
# successor would suppress still-valid feedback (and, because ``fix_committed``
# does not block the merge gate, auto-merge over it). It is therefore inherited
# only when head continuity is established -- see :func:`head_continuity_established`.
_HEAD_DEPENDENT_VERDICTS = frozenset({"fix_committed"})

# Verdicts that judge the *comment*, not the branch state, and so survive a head
# that moved: ``false_positive`` / ``defer`` / ``needs_human`` are dispositions of
# the feedback itself, and ``agent_failed`` re-queues either way.
_HEAD_INDEPENDENT_VERDICTS = frozenset(
    {
        "false_positive",
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

# A verdict key is exactly one of the three forms the adoption contract names: a
# GraphQL review-thread id (``PRRT_...``), a bare numeric review-comment id, or
# an ``issue:<numeric-id>`` issue-comment id. The pattern is closed rather than
# "any identifier-ish key", so a malformed, legacy, or future bookkeeping entry
# cannot cross the supersede boundary just by holding a verdict-shaped value.
# None of the three alternatives can begin with ``_``, which keeps every
# ``__...__`` reserved marker structurally ineligible as well.
_VERDICT_KEY_RE = re.compile(r"^(?:PRRT_[A-Za-z0-9_-]+|[0-9]+|issue:[0-9]+)$")

# Evidence markers that must travel with a copied verdict. The body hash lets the
# runner re-queue a comment whose body changed since the predecessor triaged it;
# the deferred-issue marker prevents filing a duplicate follow-up issue.
_COPIED_MARKER_PREFIXES = (
    "__review_comment_body_hash__:",
    "__deferred_issue_filed__:",
)


def head_continuity_established(
    *,
    adopted_head_sha: str | None,
    predecessor_head_sha: str | None,
) -> bool:
    """True when the adopted PR head is provably the head the predecessor processed.

    Adoption has no git or ancestry oracle in this transaction, so continuity is
    only *established* by SHA equality. Any moved head -- force-push, revert, or
    plain new commits on top -- reads as "not established", which is deliberately
    conservative: the cost is that the successor re-triages the ``fix_committed``
    items, while the alternative is inheriting a fix that may no longer exist in
    the branch and merging over the reviewer's still-open feedback.
    """
    if not adopted_head_sha or not predecessor_head_sha:
        return False
    return adopted_head_sha.strip().lower() == predecessor_head_sha.strip().lower()


def seedable_monitor_state(
    previous: Mapping[str, Any] | None,
    *,
    head_continuity: bool = False,
) -> dict[str, str]:
    """Return the allowlisted subset of a predecessor's monitor state.

    ``head_continuity`` states whether the head the successor adopts still carries
    the predecessor's processed head (:func:`head_continuity_established`). It
    fails closed: without it, the head-dependent ``fix_committed`` verdicts are
    dropped and re-triaged, while the head-independent dispositions still cross.

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
    return dict(sorted(seeded.items()))


def _is_verdict_entry(key: str, value: str, *, head_continuity: bool) -> bool:
    seedable = _SEEDABLE_VERDICTS if head_continuity else _HEAD_INDEPENDENT_VERDICTS
    return value in seedable and _VERDICT_KEY_RE.match(key) is not None


def _is_copied_marker(key: str, value: str) -> bool:
    if not value:
        return False
    return any(
        key.startswith(prefix) and len(key) > len(prefix) for prefix in _COPIED_MARKER_PREFIXES
    )
