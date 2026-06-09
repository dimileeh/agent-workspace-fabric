"""GraphQL parsing helpers for GitHub PR monitor status."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    ReviewComment,
    ReviewThreadComment,
)

if TYPE_CHECKING:
    from awf.common.github_client import _FetchedReview


def _dig(obj: Any, *keys: Any) -> Any:
    """Like ``obj.get(k1, {}).get(k2, {}) ...`` but survives lists + None."""
    cur = obj
    for k in keys:
        if cur is None:
            return None
        if isinstance(k, int):
            if not isinstance(cur, list) or k < 0 or k >= len(cur):
                return None
            cur = cur[k]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
    return cur


def _parse_check_state(value: str) -> CheckState:
    """Normalize GitHub rollup check state into `CheckState`."""
    # Rollup values per docs: EXPECTED / ERROR / FAILURE / PENDING / SUCCESS.
    upper = (value or "").upper()
    if upper == "SUCCESS":
        return CheckState.SUCCESS
    if upper in {"FAILURE", "ERROR"}:
        return CheckState.FAILURE
    if upper == "PENDING" or upper == "EXPECTED":
        return CheckState.PENDING
    return CheckState.NEUTRAL


def _parse_check_contexts(rollup: Any) -> tuple[CheckTiming, ...]:
    """Parse status contexts/check runs from GraphQL rollup payload."""
    checks: list[CheckTiming] = []
    for node in _dig(rollup, "contexts", "nodes") or []:
        if not isinstance(node, dict):
            continue
        typename = node.get("__typename")
        if typename == "StatusContext":
            name = _clean_optional_str(node.get("context"))
            if name is None:
                continue
            checks.append(
                CheckTiming(
                    name=name,
                    status=_clean_optional_str(node.get("state")),
                    details_url=_clean_optional_str(node.get("targetUrl")),
                    creator_login=_clean_optional_str(_dig(node, "creator", "login")),
                )
            )
            continue

        name = _clean_optional_str(node.get("name") or node.get("context"))
        if name is None:
            continue
        checks.append(
            CheckTiming(
                name=name,
                status=_clean_optional_str(node.get("status") or node.get("state")),
                conclusion=_clean_optional_str(node.get("conclusion")),
                started_at=_parse_github_datetime(node.get("startedAt")),
                completed_at=_parse_github_datetime(node.get("completedAt")),
                details_url=_clean_optional_str(node.get("detailsUrl") or node.get("targetUrl")),
                app_slug=_clean_optional_str(_dig(node, "checkSuite", "app", "slug")),
                app_name=_clean_optional_str(_dig(node, "checkSuite", "app", "name")),
                creator_login=_clean_optional_str(_dig(node, "checkSuite", "creator", "login")),
            )
        )
    return tuple(checks)


def _parse_review_thread_comments(
    comment_nodes: list[dict[str, Any]],
) -> tuple[ReviewThreadComment, ...]:
    """Parse GraphQL thread-comment nodes and keep timestamps for activity gating."""
    comments: list[ReviewThreadComment] = []
    for node in comment_nodes:
        database_id = node.get("databaseId")
        comments.append(
            ReviewThreadComment(
                comment_id=str(database_id) if database_id is not None else None,
                body=node.get("bodyText") or "",
                author=_clean_optional_str(_dig(node, "author", "login")),
                viewer_did_author=bool(node.get("viewerDidAuthor")),
                created_at=_parse_github_datetime(node.get("createdAt")),
                updated_at=_parse_github_datetime(node.get("updatedAt")),
                url=_clean_optional_str(node.get("url")),
            )
        )
    return tuple(comments)


def _newer_activity(
    *,
    current_at: datetime | None,
    current_source: str | None,
    candidate_at: datetime | None,
    candidate_source: str,
) -> tuple[datetime | None, str | None]:
    """Return the later activity timestamp and source across two candidates."""
    if candidate_at is None:
        return current_at, current_source
    if current_at is None or candidate_at > current_at:
        return candidate_at, candidate_source
    return current_at, current_source


def _latest_activity_from_thread_comments(
    comments: tuple[ReviewThreadComment, ...],
    *,
    current_at: datetime | None,
    current_source: str | None,
) -> tuple[datetime | None, str | None]:
    """Reduce thread comments to the newest external activity timestamp."""
    latest_at = current_at
    latest_source = current_source
    for comment in comments:
        if comment.viewer_did_author:
            continue
        latest_at, latest_source = _newer_activity(
            current_at=latest_at,
            current_source=latest_source,
            candidate_at=comment.updated_at or comment.created_at,
            candidate_source="review_thread_comment",
        )
    return latest_at, latest_source


def _latest_activity_from_reviews(
    reviews: Sequence[_FetchedReview],
    *,
    current_at: datetime | None,
    current_source: str | None,
) -> tuple[datetime | None, str | None]:
    """Reduce review payloads to the newest non-author activity timestamp."""
    latest_at = current_at
    latest_source = current_source
    for review in reviews:
        if review.viewer_did_author:
            continue
        latest_at, latest_source = _newer_activity(
            current_at=latest_at,
            current_source=latest_source,
            candidate_at=review.updated_at or review.submitted_at or review.comment.created_at,
            candidate_source="review",
        )
    return latest_at, latest_source


def _quiet_period_anchor(
    *,
    latest_external_review_activity_at: datetime | None,
    latest_external_review_activity_source: str | None,
    pr_created_at: datetime | None,
    pr_updated_at: datetime | None,
    head_committed_at: datetime | None,
) -> tuple[datetime | None, str | None]:
    """Choose the newest available anchor for the quiet-window timer."""
    anchor_at: datetime | None = latest_external_review_activity_at
    anchor_source: str | None = latest_external_review_activity_source
    candidates: list[tuple[datetime | None, str]] = [
        (pr_created_at, "pull_request"),
        (head_committed_at, "head_commit"),
    ]
    if latest_external_review_activity_at is None:
        candidates.insert(0, (pr_updated_at, "pull_request"))
    for candidate_at, candidate_source in candidates:
        anchor_at, anchor_source = _newer_activity(
            current_at=anchor_at,
            current_source=anchor_source,
            candidate_at=candidate_at,
            candidate_source=candidate_source,
        )
    return anchor_at, anchor_source


def _parse_fetched_review(node: dict[str, Any], *, fetch_index: int) -> _FetchedReview:
    """Normalize one GraphQL review node for blocking-review evaluation."""
    from awf.common.github_client import _FetchedReview

    raw_body = node.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    body_excerpt = body[:400] if body.strip() else ""
    database_id = node.get("databaseId")
    comment_id = str(database_id if database_id is not None else f"missing:{fetch_index}")
    author = _clean_optional_str(_dig(node, "author", "login"))
    submitted_at = _parse_github_datetime(node.get("submittedAt"))
    updated_at = _parse_github_datetime(node.get("updatedAt"))
    comment = ReviewComment(
        comment_id=comment_id,
        body_excerpt=body_excerpt,
        author=author,
        is_resolved=False,
        body=body,
        url=_clean_optional_str(node.get("url")),
        created_at=submitted_at,
        updated_at=updated_at,
        state=(raw_state.upper() if isinstance(raw_state := node.get("state"), str) else ""),
        source_kind="review",
        viewer_did_author=bool(node.get("viewerDidAuthor")),
    )
    return _FetchedReview(
        comment=comment,
        reviewer_key=_reviewer_effective_state_key(node, fetch_index=fetch_index),
        submitted_at=submitted_at,
        updated_at=updated_at,
        fetch_index=fetch_index,
        viewer_did_author=comment.viewer_did_author,
        has_body=bool(body.strip()),
        counts_for_required_review=_review_counts_for_required_review(node),
    )


def _reviewer_effective_state_key(node: dict[str, Any], *, fetch_index: int) -> str:
    """Build a stable per-reviewer key for review state deduplication."""
    author = _clean_optional_str(_dig(node, "author", "login"))
    if author is not None:
        return f"author:{author.lower()}"
    database_id = node.get("databaseId")
    if database_id is not None:
        return f"review:{database_id}"
    return f"review-fetch-index:{fetch_index}"


def _review_counts_for_required_review(node: dict[str, Any]) -> bool:
    """Return whether a review should participate in required-review state."""
    # Real GraphQL payloads include this Boolean. Older/fake payloads are treated
    # as counting to preserve conservative merge-gate behavior.
    # This is a push-access heuristic, not a complete GitHub branch-protection
    # required-reviewer model; read-only code-owner blocks still surface through
    # GitHub's mergeable state.
    return node.get("authorCanPushToRepository") is not False


def _effective_blocking_reviews(
    fetched_reviews: Sequence[_FetchedReview],
) -> tuple[ReviewComment, ...]:
    """Resolve latest per-reviewer state and return blockers for merge-gating."""
    # DISMISSED must be tracked so a maintainer-dismissed review overwrites that
    # reviewer's prior CHANGES_REQUESTED entry instead of leaving a stale blocker.
    effective_review_states = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
    latest_by_reviewer: dict[str, _FetchedReview] = {}
    for fetched in fetched_reviews:
        if fetched.viewer_did_author:
            continue
        if not fetched.counts_for_required_review:
            continue
        if fetched.comment.state not in effective_review_states:
            continue
        current = latest_by_reviewer.get(fetched.reviewer_key)
        if current is None or _review_is_later(fetched, current):
            latest_by_reviewer[fetched.reviewer_key] = fetched
    blockers = [
        replace(fetched.comment, blocks_merge=True)
        for fetched in sorted(latest_by_reviewer.values(), key=_review_fetch_order)
        if fetched.comment.state == "CHANGES_REQUESTED"
    ]
    return tuple(blockers)


def _review_is_later(candidate: _FetchedReview, current: _FetchedReview) -> bool:
    if (
        candidate.submitted_at is not None
        and current.submitted_at is not None
        and candidate.submitted_at != current.submitted_at
    ):
        return candidate.submitted_at > current.submitted_at
    return candidate.fetch_index > current.fetch_index


def _review_fetch_order(review: _FetchedReview) -> int:
    return review.fetch_index


def _connection_nodes(page: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node in _dig(page, "nodes") or []:
        if isinstance(node, dict):
            nodes.append(node)
    return nodes


def _extract_pr_file_paths(files_page: Any) -> list[str]:
    paths: list[str] = []
    for node in _dig(files_page, "nodes") or []:
        if not isinstance(node, dict):
            continue
        path = _clean_optional_str(node.get("path"))
        if path is not None:
            paths.append(path)
    return paths


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_mergeable(value: Any) -> MergeableState:
    upper = value.upper() if isinstance(value, str) else ""
    if upper == "MERGEABLE":
        return MergeableState.MERGEABLE
    if upper == "CONFLICTING":
        return MergeableState.CONFLICTING
    return MergeableState.UNKNOWN


def _parse_merge_state_status(value: Any) -> MergeStateStatus:
    """GraphQL returns one of: CLEAN / BEHIND / DIRTY / BLOCKED / HAS_HOOKS
    / UNSTABLE / UNKNOWN. Default to UNKNOWN for anything we don't
    recognise — decide() treats UNKNOWN as "wait, don't act"."""
    upper = value.upper() if isinstance(value, str) else ""
    try:
        return MergeStateStatus(upper)
    except ValueError:
        return MergeStateStatus.UNKNOWN


def _tail(text: str, n: int) -> str:
    """Truncate log text to the last `n` characters with a marker."""
    if len(text) <= n:
        return text
    return "…[truncated]…\n" + text[-n:]
