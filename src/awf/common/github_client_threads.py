"""GitHub in-thread reply + exhaustive review-thread resolution audit (#1018).

These are plain async functions bound onto :class:`~awf.common.github_client.GitHubClient`
as methods (``reply_to_thread = github_client_threads.reply_to_thread``), so the
client module stays under the first-party file-size guardrail while ``GitHubClient``
still satisfies the ``ForgeClient`` Protocol structurally. ``GitHubClientError`` is
imported inside each function because ``github_client`` imports this module at load
time.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client_parsing import (
    _clean_optional_str,
    _connection_nodes,
    _dig,
    _parse_github_datetime,
)
from awf.common.github_graphql import (
    _GQL_ADD_REVIEW_THREAD_REPLY,
    _GQL_REVIEW_THREAD_AUDIT,
    _GQL_REVIEW_THREAD_AUDIT_COMMENTS_PAGE,
    _GQL_REVIEW_THREAD_AUDIT_ONE,
)
from awf.common.github_retry import RetryPolicy

if TYPE_CHECKING:
    from awf.common.github_client import GitHubClient, RepoRef


async def reply_to_thread(self: GitHubClient, *, thread_id: str, body: str) -> str:
    """Post ``body`` inside review thread ``thread_id``; return the new comment id.

    A mutation, so ``RetryPolicy.NEVER``: a blind transport retry could post the
    disposition twice. Callers treat any failure as "not replied" and never resolve.
    """
    from awf.common.github_client import GitHubClientError

    payload = await self._graphql(
        query=_GQL_ADD_REVIEW_THREAD_REPLY,
        variables={"threadId": thread_id, "body": body},
        retry_policy=RetryPolicy.NEVER,
    )
    comment = _dig(payload, "data", "addPullRequestReviewThreadReply", "comment") or {}
    comment_id = _comment_id(comment)
    if not comment_id:
        raise GitHubClientError(
            operation="reply_to_thread",
            returncode=0,
            stderr="addPullRequestReviewThreadReply returned no comment id",
        )
    return comment_id


def _comment_id(comment: dict[str, Any]) -> str | None:
    """A review comment's id as ``reply_to_thread`` returns it: database id, else node id."""
    database_id = comment.get("databaseId")
    return str(database_id) if database_id is not None else _clean_optional_str(comment.get("id"))


async def fetch_review_thread_audit(
    self: GitHubClient,
    *,
    repo: RepoRef,
    pr_number: int,
    retry: bool = True,
    thread_id: str | None = None,
) -> ReviewThreadAudit:
    """Audit every thread, or one repository/PR-bound thread, with all comments.

    Fails closed (``GitHubClientError``) when a page claims ``hasNextPage`` without
    a cursor, so the gate can never certify a PR from a truncated list.
    ``retry=False`` (the in-lock re-audit) fails fast on every thread and comment
    page instead of backing off while holding the merge lock.
    """
    from awf.common.github_client import GitHubClientError

    retry_policy = RetryPolicy.READ if retry else RetryPolicy.NEVER
    if thread_id is not None:
        return await _fetch_one_thread_audit(
            self, repo=repo, pr_number=pr_number, thread_id=thread_id, retry_policy=retry_policy
        )
    threads: list[AuditedReviewThread] = []
    viewer_login: str | None = None
    cursor: str | None = None
    while True:
        variables: dict[str, Any] = {"owner": repo.owner, "repo": repo.name, "number": pr_number}
        if cursor is not None:
            variables["cursor"] = cursor
        payload = await self._graphql(
            query=_GQL_REVIEW_THREAD_AUDIT,
            variables=variables,
            retry_policy=retry_policy,
        )
        viewer_login = viewer_login or _clean_optional_str(_dig(payload, "data", "viewer", "login"))
        pr = _dig(payload, "data", "repository", "pullRequest")
        if pr is None:
            raise GitHubClientError(
                operation="fetch_review_thread_audit",
                returncode=0,
                stderr=f"PR {repo.slug()}#{pr_number} not found",
            )
        connection = pr.get("reviewThreads")
        for node in _connection_nodes(connection):
            thread = await _audited_thread(self, node, retry_policy=retry_policy)
            if thread is not None:
                threads.append(thread)
        cursor = _next_cursor(connection, operation="fetch_review_thread_audit")
        if cursor is None:
            return ReviewThreadAudit(
                supported=True,
                viewer_login=viewer_login,
                threads=tuple(threads),
            )


async def _fetch_one_thread_audit(
    self: GitHubClient,
    *,
    repo: RepoRef,
    pr_number: int,
    thread_id: str,
    retry_policy: RetryPolicy,
) -> ReviewThreadAudit:
    from awf.common.github_client import GitHubClientError

    payload = await self._graphql(
        query=_GQL_REVIEW_THREAD_AUDIT_ONE,
        variables={"threadId": thread_id},
        retry_policy=retry_policy,
    )
    node = _dig(payload, "data", "node")
    slug = _dig(node, "pullRequest", "repository", "nameWithOwner")
    if (
        not thread_id
        or thread_id != thread_id.strip()
        or not isinstance(node, dict)
        or node.get("id") != thread_id
        or _dig(node, "pullRequest", "number") != pr_number
        or not isinstance(slug, str)
        or slug.casefold() != repo.slug().casefold()
    ):
        raise GitHubClientError(
            operation="fetch_review_thread_audit",
            returncode=0,
            stderr=f"Review thread {thread_id} not found in {repo.slug()}#{pr_number}",
        )
    thread = await _audited_thread(self, node, retry_policy=retry_policy)
    assert thread is not None  # Its exact nonempty id was checked above.
    return ReviewThreadAudit(
        supported=True,
        viewer_login=_clean_optional_str(_dig(payload, "data", "viewer", "login")),
        threads=(thread,),
    )


def _next_cursor(connection: Any, *, operation: str) -> str | None:
    """Return the next page cursor, ``None`` on the last page; fail closed otherwise."""
    from awf.common.github_client import GitHubClientError

    if _dig(connection, "pageInfo", "hasNextPage") is not True:
        return None
    cursor = _clean_optional_str(_dig(connection, "pageInfo", "endCursor"))
    if cursor is None:
        raise GitHubClientError(
            operation=operation,
            returncode=0,
            stderr=f"{operation}: hasNextPage was true without an endCursor",
        )
    return cursor


async def _audited_thread(
    self: GitHubClient, node: dict[str, Any], *, retry_policy: RetryPolicy
) -> AuditedReviewThread | None:
    """Build one audited thread, following its comment pages for viewer bodies."""
    thread_id = _clean_optional_str(node.get("id"))
    if thread_id is None:
        return None
    comments = node.get("comments")
    comment_nodes = _audit_comment_nodes(comments)
    cursor = _next_cursor(comments, operation="fetch_review_thread_audit comments")
    while cursor is not None:
        payload = await self._graphql(
            query=_GQL_REVIEW_THREAD_AUDIT_COMMENTS_PAGE,
            variables={"threadId": thread_id, "cursor": cursor},
            retry_policy=retry_policy,
        )
        page = _dig(payload, "data", "node", "comments")
        comment_nodes.extend(_audit_comment_nodes(page))
        cursor = _next_cursor(page, operation="fetch_review_thread_audit comments")
    viewer_bodies: list[str] = []
    viewer_ids: list[str] = []
    viewer_posted_at: list[datetime | None] = []
    superseded = 0
    latest_edit: datetime | None = None
    for comment in comment_nodes:  # oldest first
        # AWF never edits its replies, so any edit (the shared operator identity's
        # own follow-up included) is reworded feedback.
        edited_at = _parse_github_datetime(comment.get("lastEditedAt"))
        if edited_at is not None and (latest_edit is None or edited_at > latest_edit):
            latest_edit = edited_at
        if comment.get("viewerDidAuthor") is not True:
            superseded = len(viewer_bodies)  # newer feedback outdates earlier replies
        elif isinstance(body := comment.get("body"), str):
            viewer_bodies.append(body)
            viewer_ids.append(_comment_id(comment) or "")
            viewer_posted_at.append(_parse_github_datetime(comment.get("createdAt")))
    if latest_edit is not None:
        # An edit keeps the feedback's place in the list, so a reply posted before
        # it (or not provably after it) answered the older text.
        for index, posted_at in enumerate(viewer_posted_at):
            if posted_at is None or posted_at <= latest_edit:
                superseded = max(superseded, index + 1)
    return AuditedReviewThread(
        thread_id=thread_id,
        is_resolved=node.get("isResolved") is True,
        is_outdated=node.get("isOutdated") is True,
        resolved_by=_clean_optional_str(_dig(node, "resolvedBy", "login")),
        viewer_comment_bodies=tuple(viewer_bodies),
        viewer_comment_ids=tuple(viewer_ids),
        superseded_viewer_comments=superseded,
        conversation_digest=_conversation_digest(comment_nodes),
        conversation_digest_before_reply=_conversation_digest(comment_nodes[:-1])
        if comment_nodes and comment_nodes[-1].get("viewerDidAuthor") is True
        else "",
        external_comment_bodies=tuple(
            comment.get("bodyText") or ""
            for comment in comment_nodes
            if comment.get("viewerDidAuthor") is not True
        ),
    )


def _audit_comment_nodes(connection: Any) -> list[dict[str, Any]]:
    from awf.common.github_client import GitHubClientError

    if not isinstance(connection, dict):
        raise GitHubClientError(
            operation="fetch_review_thread_audit comments",
            returncode=0,
            stderr="Review thread comment page is missing",
        )
    return _connection_nodes(connection)


def _conversation_digest(comment_nodes: list[dict[str, Any]]) -> str:
    """Fingerprint the thread's whole conversation: each comment's author side and body."""
    conversation = [
        [comment.get("viewerDidAuthor") is True, comment.get("body")] for comment in comment_nodes
    ]
    return hashlib.sha256(json.dumps(conversation).encode()).hexdigest()
