"""Provider-neutral review-thread audit snapshot for the disposition gate (#1018).

A leaf module (no runtime imports) so ``forge``, both forge clients and the PR
monitor can share it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditedReviewThread:
    """One review thread as the disposition gate needs it, resolved ones included.

    ``viewer_comment_bodies`` holds the raw markdown of comments the token's own
    identity authored (only those can carry AWF's disposition marker), oldest first.
    The first ``superseded_viewer_comments`` of them predate the thread's newest
    comment by anyone else, so they answered earlier feedback, not the current one.
    ``conversation_digest`` fingerprints every comment (author side and body, in
    order), so a new or edited comment shows up as a changed conversation.
    """

    thread_id: str
    is_resolved: bool
    is_outdated: bool
    resolved_by: str | None
    viewer_comment_bodies: tuple[str, ...] = ()
    superseded_viewer_comments: int = 0
    conversation_digest: str = ""

    @property
    def current_viewer_comment_bodies(self) -> tuple[str, ...]:
        """The viewer bodies posted after the thread's newest feedback, oldest first."""
        return self.viewer_comment_bodies[self.superseded_viewer_comments :]


@dataclass(frozen=True)
class ReviewThreadAudit:
    """Every review thread on a PR, enumerated exhaustively, plus AWF's login.

    ``supported=False`` means the forge client cannot attribute resolutions yet;
    the gate then passes and says so in its log instead of guessing.
    """

    supported: bool
    viewer_login: str | None = None
    threads: tuple[AuditedReviewThread, ...] = ()
