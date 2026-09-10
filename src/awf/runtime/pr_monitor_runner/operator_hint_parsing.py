"""Pure parsing helpers for operator remonitor hint directives.

Split out of ``operator_hints.py`` so that module stays under the first-party
line cap enforced by
``tests/unit/test_core_decomposition_maintainability.py``. Everything here is
I/O-free text handling: recognizing the feedback/thread keys an operator can
name in a guide directive, and bounding the directive copy replayed into a
re-opened thread's next comment-repair prompt.
"""

from __future__ import annotations

import re

from awf.common.redaction import redact_secrets

# Recognize the persisted review-comment key forms surfaced back to operators.
# ``issue:<databaseId>`` is already an explicit feedback key; bare databaseIds
# and Bitbucket ``bbcomment:<id>`` keys are already explicit feedback keys; bare
# databaseIds must appear with feedback/comment id context so unrelated numbers
# do not retire stale review waits.
_OPERATOR_HINT_ISSUE_FEEDBACK_ID_RE = re.compile(r"\bissue:\d+\b", re.IGNORECASE)
_OPERATOR_HINT_BITBUCKET_FEEDBACK_ID_RE = re.compile(r"\bbbcomment:\d+\b", re.IGNORECASE)
_OPERATOR_HINT_BARE_FEEDBACK_ID_RE = re.compile(
    r"""
    \b
    (?:
        feedback
        | review(?:[\s_-]+comment)?
        | comment
    )
    [\s_-]*id[\s:#-]*
    (?P<id>\d+)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Recognize the forge-neutral review-THREAD key forms an operator can name in a
# guide directive: the GitHub GraphQL review-thread node id and the Bitbucket
# thread/task encodings from ``awf.common.bitbucket_client_parsing``. The shapes
# mirror the adoption-seeding contract (``awf.service.pr_monitor_adoption_seed``)
# so both layers agree on what counts as a thread key.
#
# Matching is CASE-SENSITIVE, unlike the comment regexes above (which lowercase
# their match): GraphQL node ids and Bitbucket owner/repo slugs are
# case-significant and the extracted text is used as a literal state-map key, so
# a case-folded id would simply miss. ``bbcomment:<id>`` is deliberately absent —
# it is a comment id and stays on the comment path.
#
# The owner/repo segments accept the SAME broad shape as the decoder and the
# adoption contract (``[^/]+/[^#]+``) rather than a narrower slug whitelist: a key
# whose owner/repo carries anything outside ``[A-Za-z0-9._-]`` (for example the
# percent-encoded ``bb:acme/widgets%20legacy#12:345``) is a legal thread key, and
# failing to extract it would leave the named thread parked at ``needs_human`` so
# the monitor returns straight to the same human wait. Whitespace is the one extra
# exclusion: this regex SCANS free-form directive prose (the decoder full-matches a
# stored key), so segments stay token-local and prose like ``bb:acme/widgets, see
# PR #12:345`` cannot be stitched into a key.
_OPERATOR_HINT_REVIEW_THREAD_ID_RE = re.compile(
    r"""
    \b
    (?:
        PRRT_[A-Za-z0-9_-]+
        | bbtask:[^\s/\#]+/[^\s\#]+\#\d+:\d+\b
        | bb:[^\s/\#]+/[^\s\#]+\#\d+:\d+\b
    )
    """,
    re.VERBOSE,
)
# Cap on the operator directive stashed under ``__operator_decision__:<thread>``
# for replay into the thread's next comment-repair prompt (issue #939).
_OPERATOR_DECISION_MAX_CHARS = 1500
# Context kept ahead of the named thread id when the cap forces a window: enough
# to carry the sentence that introduces the ruling without pushing the ruling
# itself out of the tail.
_OPERATOR_DECISION_ANCHOR_LEAD_CHARS = 200
# Smallest slice worth keeping around a single mention of the thread id. It caps
# how many mentions the budget is split across, so a guide that names one thread
# dozens of times yields a few readable slices instead of unusable confetti.
_OPERATOR_DECISION_MIN_SLICE_CHARS = 200


def _operator_decision_anchor_offsets(decision: str, anchor: str | None) -> tuple[int, ...]:
    """Offsets of every whole-key mention of ``anchor`` in ``decision``, in order.

    The scan tokenizes ``decision`` with the same thread-key grammar that pulled
    the id out of the directive in the first place and keeps only a token that
    equals ``anchor``, rather than searching for the id text itself. A raw
    substring search matches a key embedded in a longer sibling id — on the tail
    (``bb:acme/widgets#12:345`` inside ``…#12:3456``) and equally on the head
    (``PRRT_a`` inside ``PRRT_zPRRT_a``) — and would window this thread's stored
    copy on the sibling's ruling while the prompt forbids re-escalation
    (PRRT_kwDOSJAM6s6fxier). Deriving the boundaries from the key grammar bounds
    both sides at once and keeps one definition of what a thread key is.

    Every mention is returned, not just the first: a guide can list the id in an
    introductory index and repeat it beside the ruling much further down
    (PRRT_kwDOSJAM6s6fx7kx).
    """
    if not anchor:
        return ()
    return tuple(
        match.start()
        for match in _OPERATOR_HINT_REVIEW_THREAD_ID_RE.finditer(decision)
        if match.group(0) == anchor
    )


def _operator_decision_windows(decision: str, offsets: tuple[int, ...]) -> list[tuple[int, int]]:
    """Slices of ``decision`` to keep, in text order, totalling at most the cap.

    Which mention carries the ruling is not decidable from the text — an index
    line and the ruling itself name the id identically — so rather than guess one
    occurrence, the budget is split evenly across the mentions and a slice is kept
    around each. Whichever mention the ruling sits beside, it survives the cap
    (PRRT_kwDOSJAM6s6fx7kx). Mentions close enough for their slices to touch are
    merged so one ruling is never chopped into two elided fragments, and with a
    single mention this is exactly the old whole-budget window.

    ``_OPERATOR_DECISION_MIN_SLICE_CHARS`` bounds how many mentions the budget is
    split across, so a guide that names one thread more often than that still has
    to drop some. It drops from the middle, keeping the earliest mention and then
    the latest ones: a ruling can equally be stated up front and merely
    cross-referenced below, or indexed up front and ruled on below, and neither
    end is decidable from the text (PRRT_kwDOSJAM6s6fyCVT).
    """
    budget = _OPERATOR_DECISION_MAX_CHARS
    if not offsets:
        return [(0, min(len(decision), budget))]
    limit = max(1, budget // _OPERATOR_DECISION_MIN_SLICE_CHARS)
    kept = offsets if len(offsets) <= limit else (offsets[0], *offsets[len(offsets) - limit + 1 :])
    share = budget // len(kept)
    lead = min(_OPERATOR_DECISION_ANCHOR_LEAD_CHARS, share // 4)
    windows: list[tuple[int, int]] = []
    for offset in kept:
        end = min(len(decision), max(0, offset - lead) + share)
        start = max(0, end - share)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            continue
        windows.append((start, end))
    return windows


def _operator_decision_marker_text(text: str, *, anchor: str | None = None) -> str:
    """Bound and redact the directive stored for a re-opened thread (issue #939).

    The stored copy is replayed verbatim into the next comment-repair prompt, so
    it is capped: an unbounded operator directive would crowd out the reviewer
    feedback the agent has to read (and bloat persisted monitor state). Secrets
    are stripped because this text becomes durable DB state, not just prompt
    input.

    ``anchor`` is the thread id this copy is stored for. A multi-thread guide can
    name a thread only past the cap, and a plain leading-prefix truncation would
    then stash a ruling meant for a *different* thread while the prompt tells the
    agent to follow it and not re-escalate (PRRT_kwDOSJAM6s6fxBwP). So when the
    directive overflows, the copy is windowed on the anchor's *whole-id* mentions
    — with a little lead-in context — instead of on the head of the text. A
    mention that only sits inside a longer sibling id does not count, and every
    mention is windowed rather than only the first, so an id that a guide indexes
    up front and repeats beside its ruling further down still carries the ruling
    (PRRT_kwDOSJAM6s6fx7kx). Elided stretches are marked with ``…`` so the agent
    can see the copy is partial. Without an anchor (or when the id does not
    survive redaction) the head window is kept.
    """
    decision = redact_secrets(text).strip()
    if len(decision) <= _OPERATOR_DECISION_MAX_CHARS:
        return decision
    windows = _operator_decision_windows(
        decision, _operator_decision_anchor_offsets(decision, anchor)
    )
    parts: list[str] = []
    previous_end = 0
    for start, end in windows:
        if start > previous_end:
            parts.append("…")
        parts.append(decision[start:end])
        previous_end = end
    if previous_end < len(decision):
        parts.append("…")
    return "".join(parts)


def _operator_hint_feedback_body_hash_key(item_id: str) -> str:
    return f"__review_comment_body_hash__:{item_id}"


def _operator_hint_feedback_id_candidates(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()
    matches: list[tuple[int, str]] = []
    matches.extend(
        (match.start(), match.group(0).lower())
        for match in _OPERATOR_HINT_ISSUE_FEEDBACK_ID_RE.finditer(text)
    )
    matches.extend(
        (match.start(), match.group(0).lower())
        for match in _OPERATOR_HINT_BITBUCKET_FEEDBACK_ID_RE.finditer(text)
    )
    matches.extend(
        (match.start("id"), match.group("id"))
        for match in _OPERATOR_HINT_BARE_FEEDBACK_ID_RE.finditer(text)
    )
    for _, item_id in sorted(matches, key=lambda candidate: candidate[0]):
        if item_id in seen:
            continue
        seen.add(item_id)
        candidates.append(item_id)
    return tuple(candidates)


def _operator_hint_review_thread_id_candidates(text: str) -> tuple[str, ...]:
    """Review-thread ids named in ``text``, deduped in first-occurrence order."""
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _OPERATOR_HINT_REVIEW_THREAD_ID_RE.finditer(text):
        thread_id = match.group(0)
        if thread_id in seen:
            continue
        seen.add(thread_id)
        candidates.append(thread_id)
    return tuple(candidates)


def _operator_hint_feedback_storage_key_candidates(referenced_id: str) -> tuple[str, ...]:
    if referenced_id.isdigit():
        if len(referenced_id) < 6:
            return (referenced_id,)
        return (referenced_id, f"issue:{referenced_id}")
    return (referenced_id,)
