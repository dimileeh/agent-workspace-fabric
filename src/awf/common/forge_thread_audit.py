"""Provider-neutral review-thread audit snapshot for the disposition gate (#1018).

A leaf module (no runtime imports) so ``forge``, both forge clients and the PR
monitor can share it without an import cycle.

Also owns the single disposition-marker regex and
:func:`is_disposition_reply` used when the status feed lists unmarked identity
comments for triage while dropping only AWF's marker-carrying replies (#1272).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Single compiled marker pattern: posting keeps a format string elsewhere;
# parsers and the disposition gate reuse this regex (#1272).
THREAD_DISPOSITION_MARKER_RE = re.compile(
    r"<!-- AWF:thread-disposition:v1 class=(?P<cls>[a-z_]+) thread=(?P<thread>\S+) -->"
)
_DISPOSITION_CLASSES = frozenset({"fixed", "fixed_branch_evidence", "false_positive", "deferred"})


def is_disposition_reply(body: str, *, thread_id: str) -> bool:
    """True only for a valid v1 marker naming ``thread_id``.

    Unmarked comments by the token identity are ordinary feedback for triage;
    only a marker-carrying reply is AWF's own disposition post (#1272).
    """
    if not body or not thread_id:
        return False
    for match in THREAD_DISPOSITION_MARKER_RE.finditer(body):
        if match.group("thread") == thread_id and match.group("cls") in _DISPOSITION_CLASSES:
            return True
    return False


@dataclass(frozen=True)
class AuditedThreadComment:
    """One review-thread comment as the bot-acknowledgement check needs it (#1138).

    ``author_is_bot`` is the forge's own account type (GitHub's ``Bot`` actor),
    never a name or text heuristic; a deleted author is not a bot.
    """

    comment_id: str
    author_login: str | None
    author_is_bot: bool
    body: str


@dataclass(frozen=True)
class AuditedReviewThread:
    """One review thread as the disposition gate needs it, resolved ones included.

    ``viewer_comment_bodies`` holds the raw markdown of comments the token's own
    identity authored (only those can carry AWF's disposition marker), oldest first;
    ``viewer_comment_ids`` holds their ids, in the same order, as ``reply_to_thread``
    returns them (``""`` when the forge gave none), so a re-read can tell the reply
    AWF just posted from an older one.
    ``viewer_comment_body_texts`` holds their plain-text bodies in that same order:
    comparable to listed ``ReviewThreadComment.body`` (also ``bodyText``) so an edit
    to an identity comment the triage already read is visible (PRRT_kwDOSJAM6s659e4O).
    The first ``superseded_viewer_comments`` of them predate the thread's newest
    comment by anyone else (or the newest edit to any comment), so they answered earlier
    feedback, not the current one.
    ``conversation_digest`` fingerprints every comment (author side and body, in
    order), so a new or edited comment shows up as a changed conversation.
    ``conversation_digest_before_reply`` is that digest without the thread's newest
    comment when the token's identity authored it (``""`` otherwise): the
    conversation that reply followed, as the thread reads now.
    ``external_comment_bodies`` holds the plain-text bodies of the comments by anyone but
    the token's identity, oldest first: the ones ``fetch_pr_status`` lists for non-viewer
    comments (it lists only an unresolved thread with remaining feedback), so an edit
    shows up there too. Unmarked viewer comments may also appear in the status feed for
    triage (#1272); they are not part of this audit field.

    For the bot-acknowledgement check (#1138), ``opener`` is the thread's first
    comment and ``trailing_comments`` are the comments posted after the newest
    one the token's identity authored, oldest first (empty without such a
    comment). ``without_trailing`` is this same thread as it would read without
    them (their edits ignored too); ``None`` when there are none. All three stay
    empty on a forge that cannot report them, which keeps today's semantics.
    """

    thread_id: str
    is_resolved: bool
    is_outdated: bool
    resolved_by: str | None
    viewer_comment_bodies: tuple[str, ...] = ()
    viewer_comment_ids: tuple[str, ...] = ()
    viewer_comment_body_texts: tuple[str, ...] = ()
    superseded_viewer_comments: int = 0
    conversation_digest: str = ""
    conversation_digest_before_reply: str = ""
    external_comment_bodies: tuple[str, ...] = ()
    opener: AuditedThreadComment | None = None
    trailing_comments: tuple[AuditedThreadComment, ...] = ()
    without_trailing: AuditedReviewThread | None = None

    @property
    def current_viewer_comment_bodies(self) -> tuple[str, ...]:
        """The viewer bodies posted after the thread's newest feedback, oldest first."""
        return self.viewer_comment_bodies[self.superseded_viewer_comments :]


@dataclass(frozen=True)
class ReviewThreadAudit:
    """An exhaustive PR audit, or one explicitly selected thread, plus AWF's login.

    ``supported=False`` means the forge client cannot attribute resolutions yet;
    the gate then passes and says so in its log instead of guessing.
    """

    supported: bool
    viewer_login: str | None = None
    threads: tuple[AuditedReviewThread, ...] = ()
