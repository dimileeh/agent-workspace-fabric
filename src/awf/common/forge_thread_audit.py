"""Provider-neutral review-thread audit snapshot for the disposition gate (#1018).

A leaf module (no runtime imports) so ``forge``, both forge clients and the PR
monitor can share it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    The first ``superseded_viewer_comments`` of them predate the thread's newest
    comment by anyone else (or the newest edit to any comment), so they answered earlier
    feedback, not the current one.
    ``conversation_digest`` fingerprints every comment (author side and body, in
    order), so a new or edited comment shows up as a changed conversation.
    ``conversation_digest_before_reply`` is that digest without the thread's newest
    comment when the token's identity authored it (``""`` otherwise): the
    conversation that reply followed, as the thread reads now.
    ``external_comment_bodies`` holds the plain-text bodies of the comments by anyone but
    the token's identity, oldest first: the ones ``fetch_pr_status`` lists, as it lists
    them (it lists only an unresolved thread with any), so an edit shows up there too.

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
