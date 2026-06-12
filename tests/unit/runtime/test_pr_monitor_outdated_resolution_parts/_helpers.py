"""Shared builders/stubs for the outdated-thread resolve-hygiene suites.

These helpers were extracted verbatim from the original
``test_pr_monitor_outdated_resolution.py`` so the suite could be split under the
1,500-line maintainability cap (the file had grown to ~1,950 lines as the #547 /
#548 reconcile cases accreted). Keeping them in one private module — rather than
duplicating them across the part files — preserves a single source of truth for
the ``ReviewThread`` shapes the tests assert over.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
)
from awf.runtime.pr_monitor_runner.outdated_resolution import _grep_id_pattern
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient


class _RecordingGitHub(DefaultMergeMethodGitHubClient):
    """Forge stub that records ``resolve_thread`` calls and optionally raises.

    The runner step is forge-neutral — it only calls ``gh.resolve_thread`` — so
    a single recording stub exercises both the GitHub and Bitbucket paths; the
    POST-not-DELETE resolve semantics are covered by the client-level tests.
    """

    def __init__(self, inner: FakeCommandRunner, *, error: Exception | None = None) -> None:
        super().__init__(inner)
        self.resolved: list[str] = []
        self.attempts: list[str] = []
        self._error = error

    async def resolve_thread(self, *, thread_id: str) -> None:
        self.attempts.append(thread_id)
        if self._error is not None:
            raise self._error
        self.resolved.append(thread_id)


def _outdated_thread(
    tid: str,
    *,
    path: str = "src/anchor.py",
    body_excerpt: str = "please fix this finding",
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path=path,
        line=7,
        body_excerpt=body_excerpt,
        author="greptile",
        is_resolved=False,
        is_outdated=True,
    )


def _outdated_thread_with_comment(
    tid: str,
    *,
    comment_at: datetime,
    viewer_did_author: bool = False,
) -> ReviewThread:
    """An outdated thread carrying one reviewer comment stamped ``comment_at``.

    Used to exercise the post-fix-activity guard: the seed compares this
    timestamp against the matching fix commit's author time.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="c1",
                body="please fix this finding",
                author="greptile",
                created_at=comment_at,
                updated_at=comment_at,
                viewer_did_author=viewer_did_author,
            ),
        ),
    )


def _outdated_thread_with_distinct_comment(
    tid: str,
    *,
    comment_id: str,
    comment_at: datetime | None = None,
) -> ReviewThread:
    """An outdated thread whose head comment carries a databaseId distinct from
    ``thread_id`` — the #547 shape.

    The fix-cycle COMMENT path records the verdict under ``comment_id`` (the
    comment's GraphQL ``databaseId``, e.g. ``4688598838``) and the commit reads
    ``fix: address review comment issue:<comment_id> — …``, while the outdated
    thread surfaces under its node ``thread_id`` (``PRRT_…``). Reader-side
    reconciliation must bridge the two.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=comment_id,
                body="please fix this finding",
                author="greptile",
                created_at=comment_at,
                updated_at=comment_at,
            ),
        ),
    )


def _outdated_thread_with_two_comments(
    tid: str,
    *,
    comment_ids: tuple[str, str],
) -> ReviewThread:
    """An outdated thread carrying two distinct-databaseId comments (#548).

    A reply comment can be addressed comment-by-comment via the fix-cycle COMMENT
    path, so a single thread can hold a verdict under each ``comment_id`` — the
    mixed-verdict shape (one resolvable, one blocking) the reconcile/seed guards
    must not resolve over.
    """
    first, second = comment_ids
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=first,
                body="please fix this finding",
                author="greptile",
            ),
            ReviewThreadComment(
                comment_id=second,
                body="and also consider this",
                author="greptile",
            ),
        ),
    )


def _outdated_thread_with_reply(
    tid: str,
    *,
    addressed_comment_id: str,
    addressed_at: datetime,
    reply_at: datetime,
) -> ReviewThread:
    """An outdated thread whose head comment was addressed comment-by-comment and
    which then gained a LATER untriaged reviewer reply (#548 / PRRT_kwDOSJAM6s6JHeA2).

    The head comment carries ``addressed_comment_id`` (the fix-cycle COMMENT path
    keys its verdict on it); the reply carries no verdict and is stamped after the
    addressed comment, so the reconcile's post-fix activity guard must treat it as
    fresh feedback.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=addressed_comment_id,
                body="please fix this finding",
                author="greptile",
                created_at=addressed_at,
                updated_at=addressed_at,
            ),
            ReviewThreadComment(
                comment_id="reply-99",
                body="actually this is still broken",
                author="greptile",
                created_at=reply_at,
                updated_at=reply_at,
            ),
        ),
    )


def _outdated_thread_with_two_handled_comments(
    tid: str,
    *,
    first_comment_id: str,
    first_at: datetime,
    second_comment_id: str,
    second_at: datetime,
) -> ReviewThread:
    """An outdated thread whose head comment AND a later reply were BOTH addressed
    comment-by-comment (#548 / PRRT_kwDOSJAM6s6JISCM).

    Both comments carry resolvable comment-keyed verdicts; the reply was created
    after the head comment. The post-fix activity guard must anchor on the NEWEST
    handled comment so an already-handled sibling is not mistaken for fresh
    feedback (which would falsely keep a fully-addressed thread open).
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=first_comment_id,
                body="please fix this finding",
                author="greptile",
                created_at=first_at,
                updated_at=first_at,
            ),
            ReviewThreadComment(
                comment_id=second_comment_id,
                body="and this related one too",
                author="greptile",
                created_at=second_at,
                updated_at=second_at,
            ),
        ),
    )


def _outdated_thread_with_edited_comment(
    tid: str,
    *,
    addressed_comment_id: str,
    created_at: datetime,
    edited_at: datetime,
) -> ReviewThread:
    """An outdated thread whose SOLE addressed comment was edited after the fix
    (#548 / PRRT_kwDOSJAM6s6JH9Zx).

    The fix-cycle COMMENT path keyed its verdict on ``addressed_comment_id``. The
    reviewer then edited that very comment (its ``updated_at`` advances past its
    ``created_at`` and its body changes), so the edit is untriaged feedback. The
    edited comment is itself the newest reviewer activity, so the post-fix guard
    must anchor on the comment's stable ``created_at`` — not its moving
    ``updated_at`` — to detect the edit.
    """
    return ReviewThread(
        thread_id=tid,
        path="src/anchor.py",
        line=7,
        body_excerpt="please fix this finding",
        author="greptile",
        is_resolved=False,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id=addressed_comment_id,
                body="actually, also handle the edited edge case",
                author="greptile",
                created_at=created_at,
                updated_at=edited_at,
            ),
        ),
    )


def _status_with_outdated(*outdated: ReviewThread) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        outdated_unresolved_inline_threads=outdated,
    )


def _resolution_events(ws: object, *, outcome: str | None = None) -> list:
    return [
        event
        for event in ws.events  # type: ignore[attr-defined]
        if event.event_type == "workspace.audit.comment_resolution"
        and (event.payload or {}).get("action") == "resolve_outdated_thread"
        and (outcome is None or (event.payload or {}).get("outcome") == outcome)
    ]


async def _call_resolve(
    runner: object,
    *,
    workspace_id: str,
    status: PRStatus,
    state: MonitorState,
) -> None:
    await runner._resolve_addressed_outdated_threads(  # type: ignore[attr-defined]
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
    )


def _grep_argv(worktree_path: Path, *ids: str) -> list[str]:
    """The bounded ``git log`` evidence grep the seeding helper issues (#484/#547).

    The id alternation OR-es every identifier the outdated thread could have been
    fixed under — the ``thread_id`` plus any review-comment databaseIds (#547) —
    each run through ``_grep_id_pattern`` (numeric ids get a non-digit boundary —
    #548) and sorted for a deterministic argv, under ``-E`` AND-ed with the literal
    ``fix: address`` prefix via ``--all-match``.
    """
    alternation = "(" + "|".join(_grep_id_pattern(i) for i in sorted(ids)) + ")"
    return [
        "git",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
        "log",
        "-n",
        "1",
        "--format=%aI",
        "-E",
        "--all-match",
        "--grep",
        "fix: address",
        "--grep",
        alternation,
        "HEAD",
    ]
