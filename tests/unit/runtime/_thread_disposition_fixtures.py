"""Shared helpers for review-thread disposition tests (#1018).

``settled_baseline_state`` starts a gate test after the rollout's first-run
legacy baseline. ``seed_resolvable_disposition`` builds the complete disposition record a
resolvable verdict needs before AWF may reply and resolve; legacy runner tests
whose fake ``_address_thread`` returns a bare verdict use it to state that
contract explicitly. ``OrderedForge`` records replies and resolves in one call
log so tests can assert "reply, then resolve" ordering. ``acked_thread`` builds
an audited thread whose opener (a review bot) replied after AWF's marker (#1138).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from awf.common.commands import AsyncCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, AuditedThreadComment
from awf.runtime.monitor_state_keys import _REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    record_item_disposition,
)
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient

_DEFAULT_COMMIT = "c" * 40
_DEFAULT_ISSUE = "https://github.com/acme/widgets/issues/7"
_DEFAULT_REASON = "the reviewed code already handles this case"
BOT_LOGIN = "review-bot"
BOT_FEEDBACK = "P2: guard the None payload"
BOT_ACK = "Agreed, this is a false positive."
# The conversation AWF's bound reply followed, as ``acked_thread`` reads it.
BOUND_DIGEST = "c" * 64


def settled_baseline_state() -> MonitorState:
    """A monitor state whose first disposition-gate run already happened (#1018).

    That first run baselines unmarked AWF resolutions as legacy and never blocks
    on them; suites testing the steady-state gate start after it, where every
    unmarked AWF resolution blocks.
    """
    state = MonitorState()
    state.mark_addressed(_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY, "[]")
    return state


def seed_resolvable_disposition(
    state: MonitorState,
    item_id: str,
    verdict: str,
    *,
    reason: str = _DEFAULT_REASON,
    commits: tuple[str, ...] = (_DEFAULT_COMMIT,),
    issue_url: str = _DEFAULT_ISSUE,
) -> ThreadDisposition:
    """Record a complete disposition for ``fix_committed`` / ``false_positive`` / ``defer``."""
    if verdict == "fix_committed":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="fixed",
            verdict=verdict,
            reason=reason,
            commits=commits,
        )
    elif verdict == "false_positive":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="false_positive",
            verdict=verdict,
            reason=reason,
        )
    elif verdict == "defer":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="deferred",
            verdict=verdict,
            reason=reason,
            issue_url=issue_url,
        )
    else:
        raise ValueError(f"{verdict!r} is not a resolvable verdict")
    record_item_disposition(state, disposition)
    return disposition


class OrderedForge(DefaultMergeMethodGitHubClient):
    """Record disposition replies and resolves in one ordered event log."""

    def __init__(
        self,
        runner: AsyncCommandRunner,
        *,
        reply_error: ForgeClientError | None = None,
        resolve_error: ForgeClientError | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(runner, **kwargs)
        self.events: list[tuple[str, ...]] = []
        self.reply_error = reply_error
        self.resolve_error = resolve_error

    async def reply_to_thread(self, *, thread_id: str, body: str) -> str:
        """Record the reply, or raise the configured forge fault."""
        if self.reply_error is not None:
            self.events.append(("reply_failed", thread_id))
            raise self.reply_error
        reply_id = await super().reply_to_thread(thread_id=thread_id, body=body)
        self.events.append(("reply", thread_id, body))
        return reply_id

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Record the resolve, or raise the configured forge fault."""
        if self.resolve_error is not None:
            self.events.append(("resolve_failed", thread_id))
            raise self.resolve_error
        self.events.append(("resolve", thread_id))

    def replies(self) -> list[tuple[str, str]]:
        """Return ``(thread_id, body)`` for every posted reply."""
        return [(event[1], event[2]) for event in self.events if event[0] == "reply"]


def thread_comment(
    body: str, *, comment_id: str, login: str | None = BOT_LOGIN, is_bot: bool = True
) -> AuditedThreadComment:
    """One audited comment; a review bot's by default."""
    return AuditedThreadComment(
        comment_id=comment_id, author_login=login, author_is_bot=is_bot, body=body
    )


def acked_thread(
    tid: str,
    marker: str,
    *,
    trailing: tuple[AuditedThreadComment, ...] | None = None,
    opener: AuditedThreadComment | None = None,
    resolved_by: str | None = "awf-operator",
    viewer_bodies: tuple[str, ...] | None = None,
    view_superseded: int = 0,
    before_reply: str = BOUND_DIGEST,
) -> AuditedReviewThread:
    """AWF replied ``marker`` and resolved; the opener then replied in the thread.

    ``without_trailing`` reads the thread as it stood right after AWF's reply:
    the marker current, its conversation ``before_reply`` (the bound digest).
    """
    trailing = trailing if trailing is not None else (thread_comment(BOT_ACK, comment_id="3"),)
    opener = opener or thread_comment(BOT_FEEDBACK, comment_id="1")
    bodies = viewer_bodies if viewer_bodies is not None else (marker,)
    view = AuditedReviewThread(
        thread_id=tid,
        is_resolved=True,
        is_outdated=False,
        resolved_by=resolved_by,
        viewer_comment_bodies=bodies,
        viewer_comment_ids=tuple(f"reply-{i + 1}" for i in range(len(bodies))),
        superseded_viewer_comments=view_superseded,
        conversation_digest="e" * 64,
        conversation_digest_before_reply=before_reply,
        external_comment_bodies=(opener.body,),
        opener=opener,
    )
    return replace(
        view,
        superseded_viewer_comments=len(bodies),
        conversation_digest="d" * 64,
        conversation_digest_before_reply="",
        external_comment_bodies=(opener.body, *(c.body for c in trailing)),
        trailing_comments=trailing,
        without_trailing=view if trailing else None,
    )
