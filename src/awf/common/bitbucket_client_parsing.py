"""Pure BitBucket-Cloud-JSON → neutral-type assembly for the PR monitor.

Mirrors the ``github_client_parsing`` split: this module holds the side-effect-free
transforms that turn BitBucket Cloud REST v2.0 payloads into the provider-neutral
``PRStatus`` / ``CheckFailure`` / ``ReviewThread`` types the monitor consumes. The
``BitBucketClient`` performs the I/O (pagination, the pipeline-log chain) and hands
already-fetched payloads here. Keeping the assembly pure keeps it trivially
unit-testable with hand-rolled BitBucket JSON, exactly like the GitHub path.

Neutral-type contract is owned by ``runtime.pr_monitor`` (do NOT change it). We only
*assemble* into it. BitBucket-specific notes that shape the mapping:

* BitBucket Cloud has no GitHub-style ``mergeStateStatus``. ``decide()`` waits forever
  on ``MergeStateStatus.UNKNOWN`` (gate 6), so an OPEN PR maps to ``CLEAN`` /
  ``MERGEABLE``; "behind base" is driven by the caller-supplied ``base_behind_count``
  (local git, same contract as GitHub) and a genuine conflict surfaces as a 409 at
  merge time rather than a pre-merge state signal.
* Review threads are reconstructed from BitBucket's flat inline-comment list by
  following ``parent`` links to a thread root; the root's ``resolution`` marks the
  thread resolved. The neutral ``thread_id`` encodes ``repo``/``pr``/``comment`` so
  ``resolve_thread`` (whose Protocol signature carries no repo/pr) can recover them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    ReviewComment,
    ReviewThread,
    ReviewThreadComment,
)

if TYPE_CHECKING:
    from awf.common.github_client import RepoRef

# ── Merge-method vocabulary mapping (issue #345 decision D5) ───────────────
# AWF speaks GitHub's three method names through the merge gate; BitBucket Cloud
# uses ``merge_commit`` / ``squash`` / ``fast_forward``. ``fast_forward`` is
# first-class (NOT aliased to rebase); the merge gate filters it out today — see
# the #352 seam flagged in the plan/PR.
_BB_TO_AWF_MERGE_METHOD: dict[str, str] = {
    "merge_commit": "merge",
    "squash": "squash",
    "fast_forward": "fast_forward",
}
_AWF_TO_BB_MERGE_STRATEGY: dict[str, str] = {
    "merge": "merge_commit",
    "squash": "squash",
    "fast_forward": "fast_forward",
}

# Neutral thread-id encoding: ``bb:<owner>/<name>#<pr>:<comment_id>``. Owner/name
# and the numeric ids never contain ``#`` or ``:``, so the parse is unambiguous.
_THREAD_ID_RE = re.compile(r"^bb:(?P<owner>[^/]+)/(?P<name>[^#]+)#(?P<pr>\d+):(?P<comment>\d+)$")


def encode_thread_id(*, repo: RepoRef, pr_number: int, comment_id: int | str) -> str:
    """Encode repo/PR/comment context into the neutral ``thread_id`` string."""
    return f"bb:{repo.owner}/{repo.name}#{pr_number}:{comment_id}"


def decode_thread_id(thread_id: str) -> tuple[str, str, int, str]:
    """Decode ``thread_id`` back into ``(owner, name, pr_number, comment_id)``.

    Raises ``ValueError`` for a thread id that was not produced by
    :func:`encode_thread_id` so ``resolve_thread`` fails loudly rather than
    POSTing to a guessed endpoint.
    """
    match = _THREAD_ID_RE.match(thread_id)
    if match is None:
        raise ValueError(f"Not a BitBucket thread id: {thread_id!r}")
    return (
        match.group("owner"),
        match.group("name"),
        int(match.group("pr")),
        match.group("comment"),
    )


def map_bb_merge_methods(strategies: Any) -> tuple[str, ...]:
    """Map BitBucket merge-strategy values to AWF method names, preserving order."""
    methods: list[str] = []
    if isinstance(strategies, list):
        for strategy in strategies:
            awf = _BB_TO_AWF_MERGE_METHOD.get(strategy) if isinstance(strategy, str) else None
            if awf is not None and awf not in methods:
                methods.append(awf)
    return tuple(methods)


def bb_merge_strategy_for_method(method: str) -> str | None:
    """Return the BitBucket strategy value for an AWF merge method, or ``None``."""
    return _AWF_TO_BB_MERGE_STRATEGY.get(method)


def parse_bb_datetime(value: Any) -> datetime | None:
    """Parse a BitBucket ISO-8601 timestamp into an aware UTC ``datetime``."""
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


def _clean_optional_str(value: Any) -> str | None:
    """Return trimmed string content or ``None`` for empty/non-string values."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _comment_author(comment: dict[str, Any]) -> str | None:
    """Resolve a display name for a BitBucket comment/PR user object."""
    user = comment.get("user")
    if not isinstance(user, dict):
        return None
    return _clean_optional_str(user.get("display_name") or user.get("nickname"))


def _comment_account_id(comment: dict[str, Any]) -> str | None:
    """Return the BitBucket ``account_id`` of a comment's author, if present."""
    user = comment.get("user")
    if not isinstance(user, dict):
        return None
    return _clean_optional_str(user.get("account_id") or user.get("uuid"))


def html_href(obj: Any) -> str | None:
    """Return ``links.html.href`` from a BitBucket resource object, if present."""
    if not isinstance(obj, dict):
        return None
    links = obj.get("links")
    if not isinstance(links, dict):
        return None
    html = links.get("html")
    if not isinstance(html, dict):
        return None
    return _clean_optional_str(html.get("href"))


def _comment_url(comment: dict[str, Any]) -> str | None:
    """Return the ``links.html.href`` for a BitBucket comment, if present."""
    return html_href(comment)


def parse_pr_terminal_state(pr: dict[str, Any]) -> tuple[bool, bool, str | None]:
    """Return ``(merged, closed, merge_commit_sha)`` from a BitBucket PR payload.

    BitBucket PR ``state`` ∈ {OPEN, MERGED, DECLINED, SUPERSEDED}. MERGED maps to
    ``merged`` (gate 0 → ShortCircuitCompleted); DECLINED/SUPERSEDED map to
    ``closed`` (gate 0 → Abort(pr_closed_externally)).
    """
    state = str(pr.get("state") or "").upper()
    merge_commit = pr.get("merge_commit")
    merge_commit_sha = (
        _clean_optional_str(merge_commit.get("hash")) if isinstance(merge_commit, dict) else None
    )
    merged = state == "MERGED" or merge_commit_sha is not None
    closed = (not merged) and state in {"DECLINED", "SUPERSEDED"}
    return merged, closed, merge_commit_sha


def mergeable_state_for(*, merged: bool, closed: bool) -> MergeableState:
    """Map BitBucket terminal-state flags to a neutral ``MergeableState``.

    BitBucket Cloud exposes no pre-merge conflict flag on the PR GET, so an OPEN
    PR is reported ``MERGEABLE`` (a real conflict surfaces as a 409 at merge time
    and the merge loop falls back to NotifyHuman). Terminal PRs are ``UNKNOWN``.
    """
    if merged or closed:
        return MergeableState.UNKNOWN
    return MergeableState.MERGEABLE


def merge_state_status_for(*, merged: bool, closed: bool) -> MergeStateStatus:
    """Map BitBucket terminal-state flags to a neutral ``MergeStateStatus``.

    ``decide()`` waits forever on ``UNKNOWN`` (gate 6), so an OPEN PR maps to
    ``CLEAN``. "Behind base" is driven by the caller-supplied ``base_behind_count``
    rather than this signal, mirroring GitHub's local rev-list contract.
    """
    if merged or closed:
        return MergeStateStatus.UNKNOWN
    return MergeStateStatus.CLEAN


# BitBucket build-status states per the commit-statuses endpoint. Known
# in-progress states (INPROGRESS/PENDING) need no allow-list of their own: any
# state that is neither FAILED nor explicitly SUCCESSFUL is treated as not-yet-
# green, which also covers unrecognized/new BitBucket states conservatively.
_BB_STATUS_FAILED = {"FAILED", "STOPPED"}
_BB_STATUS_SUCCESS = {"SUCCESSFUL"}


def parse_check_state(statuses: list[dict[str, Any]]) -> CheckState:
    """Reduce BitBucket commit build-statuses to a neutral aggregate ``CheckState``.

    No statuses reported → PENDING; any FAILED/STOPPED → FAILURE; else
    SUCCESS only when *every* status is explicitly SUCCESSFUL; otherwise
    PENDING.

    An empty status page is treated as PENDING rather than SUCCESS: a freshly
    pushed PR has no commit status until Pipelines posts one, and reporting
    SUCCESS there would let ``decide()`` skip the WaitForCI gate and merge
    before any check completed. This mirrors the GitHub path, which defaults a
    missing rollup to PENDING (see ``github_client`` ``fetch_pr_status``).

    SUCCESS requires an allow-list match (``_BB_STATUS_SUCCESS``): an
    unrecognized or newly introduced BitBucket state is treated as
    not-yet-green (PENDING) rather than silently passing the WaitForCI gate.
    """
    if not statuses:
        return CheckState.PENDING
    saw_unfinished = False
    for status in statuses:
        state = str(status.get("state") or "").upper()
        if state in _BB_STATUS_FAILED:
            return CheckState.FAILURE
        if state not in _BB_STATUS_SUCCESS:
            saw_unfinished = True
    if saw_unfinished:
        return CheckState.PENDING
    return CheckState.SUCCESS


def parse_check_timings(statuses: list[dict[str, Any]]) -> tuple[CheckTiming, ...]:
    """Parse BitBucket commit build-statuses into neutral ``CheckTiming`` rows."""
    timings: list[CheckTiming] = []
    for status in statuses:
        name = _clean_optional_str(status.get("name") or status.get("key"))
        if name is None:
            continue
        timings.append(
            CheckTiming(
                name=name,
                status=_clean_optional_str(status.get("state")),
                conclusion=_clean_optional_str(status.get("state")),
                started_at=parse_bb_datetime(status.get("created_on")),
                completed_at=parse_bb_datetime(status.get("updated_on")),
                details_url=_clean_optional_str(status.get("url")),
            )
        )
    return tuple(timings)


def _comment_is_inline(comment: dict[str, Any]) -> bool:
    return isinstance(comment.get("inline"), dict)


def _comment_parent_id(comment: dict[str, Any]) -> str | None:
    parent = comment.get("parent")
    if isinstance(parent, dict) and parent.get("id") is not None:
        return str(parent["id"])
    return None


def _comment_is_resolved(comment: dict[str, Any]) -> bool:
    return isinstance(comment.get("resolution"), dict)


def _thread_comment(comment: dict[str, Any], *, account_id: str | None) -> ReviewThreadComment:
    raw = comment.get("content")
    body = raw.get("raw") if isinstance(raw, dict) else None
    return ReviewThreadComment(
        comment_id=str(comment["id"]) if comment.get("id") is not None else None,
        body=body or "",
        author=_comment_author(comment),
        created_at=parse_bb_datetime(comment.get("created_on")),
        updated_at=parse_bb_datetime(comment.get("updated_on")),
        url=_comment_url(comment),
        viewer_did_author=_is_viewer(comment, account_id),
    )


def _is_viewer(comment: dict[str, Any], account_id: str | None) -> bool:
    return account_id is not None and _comment_account_id(comment) == account_id


def _resolve_thread_root_id(comment: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str:
    """Follow ``parent`` links to the thread root, guarding against cycles."""
    current = comment
    seen: set[str] = set()
    while True:
        parent_id = _comment_parent_id(current)
        if parent_id is None or parent_id in seen or parent_id not in by_id:
            return str(current["id"])
        seen.add(parent_id)
        current = by_id[parent_id]


def build_review_threads(
    comments: list[dict[str, Any]],
    *,
    repo: RepoRef,
    pr_number: int,
    account_id: str | None,
) -> tuple[ReviewThread, ...]:
    """Reconstruct inline review threads from BitBucket's flat comment list.

    Mirrors the GitHub assembly: resolved threads and viewer-authored comments are
    dropped, and a thread with no remaining external comments is skipped. The
    neutral ``thread_id`` encodes repo/PR/comment so ``resolve_thread`` can recover
    BitBucket context from the Protocol's repo-less signature.
    """
    inline = [
        comment
        for comment in comments
        if _comment_is_inline(comment)
        and not comment.get("deleted")
        and comment.get("id") is not None
    ]
    by_id = {str(comment["id"]): comment for comment in inline}
    roots: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for comment in inline:
        root_id = _resolve_thread_root_id(comment, by_id)
        grouped.setdefault(root_id, []).append(comment)
        if root_id == str(comment["id"]):
            roots[root_id] = comment

    threads: list[ReviewThread] = []
    for root_id, members in grouped.items():
        root = roots.get(root_id, members[0])
        if any(_comment_is_resolved(member) for member in members):
            continue
        ordered = sorted(
            members,
            key=lambda member: (
                parse_bb_datetime(member.get("created_on")) or datetime.min.replace(tzinfo=UTC),
                str(member["id"]),
            ),
        )
        all_comments = tuple(_thread_comment(member, account_id=account_id) for member in ordered)
        external = tuple(comment for comment in all_comments if not comment.viewer_did_author)
        if not external:
            continue
        first = external[0]
        inline_meta = root.get("inline") if isinstance(root.get("inline"), dict) else {}
        # Mirror the GitHub path (github_client.py): an inline comment BitBucket
        # marks ``outdated`` after a new push no longer describes the current
        # diff. Downstream gates treat every unresolved thread as actionable and
        # never consult ``is_outdated``, so keeping it here would drive needless
        # fix cycles and block merges. Drop it as GitHub drops ``isOutdated``.
        if isinstance(inline_meta, dict) and bool(inline_meta.get("outdated")):
            continue
        line = inline_meta.get("to") if isinstance(inline_meta, dict) else None
        if line is None and isinstance(inline_meta, dict):
            line = inline_meta.get("from")
        threads.append(
            ReviewThread(
                thread_id=encode_thread_id(repo=repo, pr_number=pr_number, comment_id=root_id),
                path=_clean_optional_str(inline_meta.get("path"))
                if isinstance(inline_meta, dict)
                else None,
                line=line if isinstance(line, int) else None,
                body_excerpt=(first.body or "")[:400],
                author=first.author,
                is_resolved=False,
                comments=external,
                url=first.url,
                is_outdated=False,
            )
        )
    return tuple(threads)


def build_general_review_comments(
    comments: list[dict[str, Any]],
    *,
    account_id: str | None,
) -> tuple[ReviewComment, ...]:
    """Map BitBucket top-level (non-inline) PR comments to neutral ``ReviewComment``s.

    Mirrors GitHub's issue-comment handling: viewer-authored, deleted, and empty
    comments are dropped so AWF never triages its own notifications.
    """
    reviews: list[ReviewComment] = []
    for comment in comments:
        if _comment_is_inline(comment) or comment.get("deleted") or comment.get("id") is None:
            continue
        if _is_viewer(comment, account_id):
            continue
        raw = comment.get("content")
        body = raw.get("raw") if isinstance(raw, dict) else None
        if not body or not body.strip():
            continue
        created_at = parse_bb_datetime(comment.get("created_on"))
        updated_at = parse_bb_datetime(comment.get("updated_on"))
        reviews.append(
            ReviewComment(
                comment_id=f"bbcomment:{comment['id']}",
                body_excerpt=body[:400],
                author=_comment_author(comment),
                is_resolved=False,
                body=body,
                url=_comment_url(comment),
                created_at=created_at,
                updated_at=updated_at,
                source_kind="issue",
                viewer_did_author=False,
            )
        )
    return tuple(reviews)


def latest_external_review_activity(
    comments: list[dict[str, Any]],
    *,
    account_id: str | None,
) -> tuple[datetime | None, str | None]:
    """Return the newest external (non-viewer) comment activity timestamp + source.

    Mirrors the GitHub status path's ``_latest_activity_from_thread_comments``: every
    non-deleted comment the viewer did not author counts as review activity — inline
    *and* top-level, resolved threads included — so the non-check-reviewer quiet window
    re-anchors on a late Bitbucket reviewer comment instead of decaying to the
    head-only fallback (which would let a fresh comment be merged past immediately).
    """
    latest_at: datetime | None = None
    latest_source: str | None = None
    for comment in comments:
        if comment.get("deleted") or comment.get("id") is None:
            continue
        if _is_viewer(comment, account_id):
            continue
        candidate = parse_bb_datetime(comment.get("updated_on")) or parse_bb_datetime(
            comment.get("created_on")
        )
        if candidate is None:
            continue
        source = "review_thread_comment" if _comment_is_inline(comment) else "issue_comment"
        if latest_at is None or candidate > latest_at:
            latest_at, latest_source = candidate, source
    return latest_at, latest_source


def build_blocking_reviews(
    pr: dict[str, Any],
    *,
    account_id: str | None,
) -> tuple[ReviewComment, ...]:
    """Map BitBucket "Request changes" reviewers to merge-blocking reviews.

    BitBucket Cloud records a reviewer's *Request changes* decision on the PR
    ``participants`` entry (``state == "changes_requested"``), not in the
    ``/comments`` list — so a reviewer can block a PR without leaving an
    unresolved comment. ``decide()``'s review-blocker gate consults
    ``PRStatus.blocking_reviews``; without this the BitBucket path would proceed
    to merge despite an active requested-changes review. Viewer-authored states
    are excluded so AWF never blocks on its own account, mirroring the GitHub
    ``_effective_blocking_reviews`` contract (latest state per reviewer).
    """
    participants = pr.get("participants")
    if not isinstance(participants, list):
        return ()
    blockers: list[ReviewComment] = []
    seen: set[str] = set()
    for participant in participants:
        if not isinstance(participant, dict):
            continue
        if str(participant.get("state") or "").strip().lower() != "changes_requested":
            continue
        if _is_viewer(participant, account_id):
            continue
        reviewer_id = _comment_account_id(participant)
        key = reviewer_id or _comment_author(participant) or f"__participant_{len(blockers)}"
        if key in seen:
            continue
        seen.add(key)
        blockers.append(
            ReviewComment(
                comment_id=f"bbreview:{key}",
                body_excerpt="Reviewer requested changes",
                author=_comment_author(participant),
                is_resolved=False,
                blocks_merge=True,
                state="CHANGES_REQUESTED",
                source_kind="review",
                viewer_did_author=False,
            )
        )
    return tuple(blockers)


def extract_diffstat_paths(entries: list[dict[str, Any]]) -> tuple[str, ...]:
    """Collect changed file paths from BitBucket PR diffstat entries."""
    paths: list[str] = []
    for entry in entries:
        for side in ("new", "old"):
            node = entry.get(side)
            if isinstance(node, dict):
                path = _clean_optional_str(node.get("path"))
                if path is not None and path not in paths:
                    # Collect both sides so rename/move entries (new.path !=
                    # old.path) contribute the source path too; the membership
                    # check de-duplicates ordinary modifications where the sides
                    # match. Dropping the source path would let a rename out of
                    # an owned/protected path evade scope/staleness checks.
                    paths.append(path)
    return tuple(paths)


def _tail(text: str, n: int) -> str:
    """Truncate log text to the last ``n`` characters with a marker."""
    if len(text) <= n:
        return text
    return "…[truncated]…\n" + text[-n:]
