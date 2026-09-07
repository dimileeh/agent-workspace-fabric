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
        | bbtask:[^\s/\#]+/[^\s\#]+\#\d+:\d+
        | bb:[^\s/\#]+/[^\s\#]+\#\d+:\d+
    )
    """,
    re.VERBOSE,
)
# Cap on the operator directive stashed under ``__operator_decision__:<thread>``
# for replay into the thread's next comment-repair prompt (issue #939).
_OPERATOR_DECISION_MAX_CHARS = 1500


def _operator_decision_marker_text(text: str) -> str:
    """Bound and redact the directive stored for a re-opened thread (issue #939).

    The stored copy is replayed verbatim into the next comment-repair prompt, so
    it is capped: an unbounded operator directive would crowd out the reviewer
    feedback the agent has to read (and bloat persisted monitor state). Secrets
    are stripped because this text becomes durable DB state, not just prompt
    input.
    """
    decision = redact_secrets(text).strip()
    if len(decision) <= _OPERATOR_DECISION_MAX_CHARS:
        return decision
    return f"{decision[:_OPERATOR_DECISION_MAX_CHARS]}…"


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
