"""GitHub client — thin wrappers over ``gh`` CLI + GraphQL mutations.

Every call is routed through an ``AsyncCommandRunner``. Production uses
``AsyncioSubprocessRunner``; tests inject ``FakeCommandRunner`` and queue
canned responses. No direct ``subprocess`` or ``requests`` calls in this
module — lets the runner be mocked at one seam and keeps the monitor
loop deterministic under test.

Scope for Phase 1.5 of AWF:

* ``fetch_pr_status`` — one call, returns the fully assembled ``PRStatus``
  (see ``pr_monitor.py``) by combining ``gh pr view --json …`` with a
  GraphQL query for review threads + review-level comments. Keeping the
  parsing in one place means fake data only has to match one shape.
* ``resolve_thread`` — posts the ``resolveReviewThread`` GraphQL mutation.
* ``post_comment`` — generic ``gh pr comment`` for reply-replies and
  release-PR "ready to merge" notifications.
* ``merge_pr`` — ``gh pr merge …``.

The repo argument is passed explicitly each call so the client is
stateless and thread-safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner
from awf.common.forge_errors import ForgeClientError
from awf.common.github_graphql import (
    _GQL_PR_FILES_PAGE,
    _GQL_PR_ISSUE_COMMENTS_PAGE,
    _GQL_PR_REVIEW_THREADS_PAGE,
    _GQL_PR_REVIEWS_PAGE,
    _GQL_PR_STATE,
    _GQL_RESOLVE_THREAD,
    _GQL_REVIEW_THREAD_COMMENTS_PAGE,
)
from awf.common.logging import get_logger
from awf.db.enums import ForgeKind
from awf.runtime.ci_failure_evidence import extract_ci_failure_evidence, redact_ci_log
from awf.runtime.pr_monitor import (
    CheckFailure,
    PRStatus,
    ReviewComment,
    ReviewThread,
)

_log = get_logger(__name__)

# ``gh pr create`` prints the PR URL as the only non-empty, non-warning line of
# stdout. Searching (rather than full-match) tolerates leading "Creating pull
# request..." status noise from current/future gh versions without persisting a
# non-URL string as the PR URL — the caller stores this verbatim and downstream
# PR-number extraction would otherwise silently yield ``None`` and break the
# monitor handoff.
_PR_URL_PATTERN = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


# GitHub shells ``gh`` and carries no HTTP status, so it has no native per-fault
# reason code. The shared ``ForgeClientError`` contract requires a stable
# ``reason_code`` for every forge fault, so GitHub faults default to this code (the
# PR monitor classifies transient-vs-deterministic from stderr markers, not this
# code). Defined here, in a client file, so the reason catalog picks it up.
GITHUB_API_ERROR = "GITHUB_API_ERROR"


class GitHubClientError(ForgeClientError):
    """Raised when ``gh`` or GraphQL returns a non-zero exit / error payload."""

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stderr: str,
        reason_code: str = GITHUB_API_ERROR,
    ) -> None:
        """Store redacted command failure context for monitor diagnostics."""
        self.operation = operation
        self.returncode = returncode
        self.reason_code = reason_code
        self.stderr = redact_audit_text(stderr)
        super().__init__(
            f"{operation} failed (exit={returncode}): {self.stderr.strip() or '<no output>'}"
        )

    def redacted_detail(self) -> str:
        """Return the redacted gh stderr — GitHub's human-facing failure detail."""
        return self.stderr

    def merge_method_stderr(self) -> str:
        """Return the gh stderr the merge-method-mismatch parser inspects."""
        return self.stderr


class PullRequestMetadataError(Exception):
    """Structured failure while resolving static PR adoption metadata."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.message = message
        self.detail = detail
        super().__init__(message)


@dataclass(frozen=True)
class _FetchedReview:
    comment: ReviewComment
    reviewer_key: str
    submitted_at: datetime | None
    updated_at: datetime | None
    fetch_index: int
    viewer_did_author: bool
    has_body: bool
    counts_for_required_review: bool


# Forge → canonical hostname. Used by ``RepoRef`` detection + URL builders so a
# single mapping keeps parsing and emission in lockstep (issue #345 Phase 1).
_FORGE_HOSTS: dict[ForgeKind, str] = {
    "github": "github.com",
    "bitbucket": "bitbucket.org",
}
_HOST_FORGES: dict[str, ForgeKind] = {host: forge for forge, host in _FORGE_HOSTS.items()}


@dataclass(frozen=True)
class RepoRef:
    """Owner + repo name parsed out of URLs like
    ``git@github.com:org/repo.git`` or ``https://github.com/org/repo``.

    ``forge`` records which code-forge the ref was detected on (``github`` by
    default; ``bitbucket`` for ``bitbucket.org`` URLs). The URL builders are
    host-aware off ``forge`` so a BitBucket ref emits ``bitbucket.org`` URLs.
    GitHub behavior is unchanged when ``forge == "github"`` (the common path)."""

    owner: str
    name: str
    forge: ForgeKind = "github"

    @classmethod
    def from_url(cls, repo_url: str) -> RepoRef:
        """Parse a repository URL/slug into a `RepoRef`, detecting the forge by host.

        Recognized hosts: ``github.com`` (forge ``github``) and ``bitbucket.org``
        (forge ``bitbucket``). A bare ``owner/repo`` slug (no host, no scheme)
        defaults to GitHub. Any other host preserves the existing ``ValueError``.
        """
        value = repo_url.strip()
        # Bare ``owner/repo`` slug (no host, no scheme) defaults to GitHub.
        slug_match = re.fullmatch(r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?", value)
        if (
            slug_match
            and "github.com" not in value
            and "bitbucket.org" not in value
            and ":" not in value
        ):
            return cls(owner=slug_match.group(1), name=slug_match.group(2), forge="github")

        # SSH scp-like form: ``git@<host>:owner/repo(.git)?``.
        for host, forge in _HOST_FORGES.items():
            ssh_match = re.fullmatch(rf"git@{re.escape(host)}:([^/]+)/([^/]+?)(?:\.git)?/?", value)
            if ssh_match:
                return cls(owner=ssh_match.group(1), name=ssh_match.group(2), forge=forge)

        parsed = urlsplit(value)
        parsed_host = parsed.hostname.lower() if parsed.hostname is not None else None
        url_forge = _HOST_FORGES.get(parsed_host) if parsed_host is not None else None
        is_http_url = parsed.scheme in {"http", "https"}
        is_ssh_url = parsed.scheme == "ssh" and (
            parsed.username is None or parsed.username.lower() == "git"
        )
        if url_forge is not None and (is_http_url or is_ssh_url):
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0] and parts[1]:
                name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
                if name:
                    return cls(owner=parts[0], name=name, forge=url_forge)
            # Preserve the original byte-for-byte message on the github-cannot-parse
            # path (plan contract); other forges name their host for a clear error.
            label = "GitHub" if url_forge == "github" else _FORGE_HOSTS[url_forge]
            raise ValueError(f"Cannot parse {label} repo from URL: {repo_url!r}")

        raise ValueError(f"Cannot parse repo from URL: {repo_url!r}")

    def _forge_host(self) -> str:
        """Return the canonical hostname for this ref's forge."""
        return _FORGE_HOSTS[self.forge]

    def slug(self) -> str:
        """Return the repository slug in `owner/name` format."""
        return f"{self.owner}/{self.name}"

    def https_url(self) -> str:
        """Return HTTPS clone URL for the repository."""
        return f"https://{self._forge_host()}/{self.owner}/{self.name}.git"

    def ssh_url(self) -> str:
        """Return SSH clone URL for the repository."""
        return f"git@{self._forge_host()}:{self.owner}/{self.name}.git"

    def clone_url_like(self, repo_url: str) -> str:
        """Return a clone URL matching the requested transport style."""
        host = self._forge_host()
        stripped = repo_url.strip()
        if stripped.startswith(f"git@{host}:") or stripped.startswith(f"ssh://git@{host}/"):
            return self.ssh_url()

        parsed = urlsplit(stripped)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.lower() == host
        ):
            userinfo, sep, _host = parsed.netloc.rpartition("@")
            if sep and userinfo:
                return f"https://{userinfo}@{host}/{self.owner}/{self.name}.git"
            return self.https_url()

        return self.https_url()


@dataclass(frozen=True)
class PullRequestAdoptionMetadata:
    """Static GitHub PR metadata needed to adopt an existing PR monitor."""

    number: int
    head_ref: str
    head_repo_slug: str
    base_ref: str
    head_sha: str
    base_sha: str
    state: str
    is_draft: bool
    closed: bool
    merged: bool
    author: str | None
    url: str
    title: str


@dataclass(frozen=True)
class BranchOpenPullRequest:
    """Open PR metadata resolved from a head branch."""

    url: str
    number: int
    head_ref: str
    head_repo_slug: str
    head_sha: str | None = None


_PR_ADOPTION_VIEW_JSON_FIELDS = (
    "number,headRefName,headRepository,isCrossRepository,baseRefName,"
    "headRefOid,baseRefOid,state,isDraft,author,url,title"
)
_BRANCH_OPEN_PR_LIST_JSON_FIELDS = (
    "number,url,headRefName,headRefOid,headRepository,headRepositoryOwner"
)
_BRANCH_OPEN_PR_LIST_LIMIT = 1000


def parse_github_pull_request_url(pr_url: str) -> tuple[RepoRef, int]:
    """Parse a canonical GitHub PR URL into ``(repo, number)``."""

    parsed = urlsplit(pr_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    try:
        number = int(parts[3])
    except ValueError as exc:
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}") from exc
    if number <= 0:
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    return RepoRef(owner=parts[0], name=parts[1]), number


async def fetch_pull_request_adoption_metadata(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    pr_number: int,
) -> PullRequestAdoptionMetadata:
    """Fetch one-shot metadata for adopting an existing GitHub PR."""

    result = await runner.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo.slug(),
            "--json",
            _PR_ADOPTION_VIEW_JSON_FIELDS,
        ]
    )
    if result.returncode != 0:
        reason = (
            "PR_NOT_FOUND"
            if _looks_like_missing_pr_error(result.stderr)
            else "PR_METADATA_FETCH_FAILED"
        )
        raise PullRequestMetadataError(
            reason_code=reason,
            message=(result.stderr or f"gh pr view exited {result.returncode}").strip(),
            detail={
                "repo_slug": repo.slug(),
                "pr_number": pr_number,
                "returncode": result.returncode,
            },
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"failed to parse gh pr view JSON: {exc}",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        ) from exc

    return _parse_pull_request_adoption_metadata(payload, repo=repo, pr_number=pr_number)


async def list_open_pull_requests_for_branch(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    branch_name: str,
    base_branch: str | None = None,
) -> list[BranchOpenPullRequest]:
    """List open GitHub PRs whose head branch matches ``branch_name``."""

    stripped_branch = branch_name.strip()
    if not stripped_branch:
        return []
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo.slug(),
        "--head",
        stripped_branch,
        "--state",
        "open",
        "--limit",
        str(_BRANCH_OPEN_PR_LIST_LIMIT),
    ]
    if base_branch is not None and base_branch.strip():
        command.extend(["--base", base_branch.strip()])
    command.extend(["--json", _BRANCH_OPEN_PR_LIST_JSON_FIELDS])
    result = await runner.run(command)
    if result.returncode != 0:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_FAILED",
            message=(result.stderr or f"gh pr list exited {result.returncode}").strip(),
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
                "returncode": result.returncode,
            },
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"failed to parse gh pr list JSON: {exc}",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
            },
        ) from exc
    if not isinstance(payload, list):
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list returned non-list JSON.",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
            },
        )
    results: list[BranchOpenPullRequest] = []
    parse_failures: list[tuple[int, PullRequestMetadataError]] = []
    for index, item in enumerate(payload):
        try:
            results.append(
                _parse_branch_open_pull_request(item, repo=repo, branch_name=stripped_branch)
            )
        except PullRequestMetadataError as exc:
            parse_failures.append((index, exc))

    failure_summaries: list[dict[str, object]] = []
    for index, parse_error in parse_failures:
        error = redact_audit_text(parse_error.message)
        failure_summaries.append(
            {
                "item_index": index,
                "reason_code": parse_error.reason_code,
                "error": error,
            }
        )
        _log.warning(
            "github.open_pr_item_parse_failed",
            repo_slug=repo.slug(),
            branch_name=stripped_branch,
            item_index=index,
            reason_code=parse_error.reason_code,
            error=error,
        )
    if parse_failures and not results:
        failure_count = len(parse_failures)
        item_label = "item" if failure_count == 1 else "items"
        _log.warning(
            "github.open_pr_batch_parse_failed",
            repo_slug=repo.slug(),
            branch_name=stripped_branch,
            base_branch=base_branch,
            failure_count=failure_count,
            failures=failure_summaries,
        )
        if failure_count == 1:
            raise parse_failures[0][1]
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"failed to parse {failure_count} gh pr list {item_label}.",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
                "failure_count": failure_count,
                "failures": failure_summaries,
            },
        ) from parse_failures[0][1]
    return results


class BranchOpenPullRequestResolver:
    """Resolve open PRs for a branch using the GitHub CLI."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        """Store the command runner used for GH CLI queries."""
        self._runner = runner

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[BranchOpenPullRequest]:
        """Resolve open PRs for a branch, optionally scoped by base branch."""
        try:
            repo = RepoRef.from_url(repo_url)
        except ValueError as exc:
            redacted_repo_url = redact_audit_text(repo_url)
            redacted_error = redact_audit_text(str(exc))
            _log.warning(
                "github.open_pr_lookup_skipped_invalid_repo_url",
                repo_url=redacted_repo_url,
                branch_name=branch_name,
                base_branch=base_branch,
                error=redacted_error,
            )
            raise PullRequestMetadataError(
                reason_code="OPEN_PR_LOOKUP_INVALID",
                message=f"cannot parse repo_url for open PR lookup: {redacted_error}",
                detail={
                    "repo_url": redacted_repo_url,
                    "branch_name": branch_name,
                    "base_branch": base_branch,
                },
            ) from exc
        return await list_open_pull_requests_for_branch(
            runner=self._runner,
            repo=repo,
            branch_name=branch_name,
            base_branch=base_branch,
        )


class GitHubClient:
    """Stateless façade over ``gh`` CLI + GraphQL. Re-entrant."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        """Store the shared command runner for all GitHub operations."""
        self._runner = runner

    async def aclose(self) -> None:
        """No-op: a ``GitHubClient`` owns no closable resource.

        It wraps a stateless ``AsyncCommandRunner`` (each ``gh`` call is its own
        subprocess), so there is no connection pool to release. This satisfies the
        :class:`~awf.common.forge.ForgeClient` ``aclose`` contract so callers can
        close any forge client uniformly, without branching on the concrete forge.
        """

    async def __aenter__(self) -> GitHubClient:
        """Enter an ``async with`` block, returning this client unchanged."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit an ``async with`` block (no-op; see :meth:`aclose`)."""
        await self.aclose()

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        """Single GraphQL round-trip; assembles a ``PRStatus``.

        ``base_behind_count`` is computed by the caller via local git
        (``git rev-list --count HEAD..origin/<base>``). GitHub's
        ``mergeable`` field tells us *whether* there's a conflict but not
        the count of commits behind, so we pass it in.
        """
        payload = await self._graphql(
            query=_GQL_PR_STATE,
            variables={
                "owner": repo.owner,
                "repo": repo.name,
                "number": pr_number,
            },
        )
        pr = payload["data"]["repository"]["pullRequest"]
        if pr is None:
            raise GitHubClientError(
                operation="fetch_pr_status",
                returncode=0,
                stderr=f"PR {repo.slug()}#{pr_number} not found",
            )

        # ── Check state ────────────────────────────────────────────────
        rollup = _dig(pr, "commits", "nodes", 0, "commit", "statusCheckRollup")
        check_state_str = (rollup or {}).get("state") or "PENDING"
        check_state = _parse_check_state(check_state_str)
        checks = _parse_check_contexts(rollup)
        if _dig(rollup, "contexts", "pageInfo", "hasNextPage") is True:
            _log.warning(
                "github.check_contexts_truncated",
                repo=repo.slug(),
                pr_number=pr_number,
                fetched_contexts_limit=100,
            )

        # ── Mergeable ──────────────────────────────────────────────────
        mergeable = _parse_mergeable(pr.get("mergeable"))
        merge_state_status = _parse_merge_state_status(pr.get("mergeStateStatus"))
        latest_review_activity_at: datetime | None = None
        latest_review_activity_source: str | None = None

        # ── Review threads: inline ─────────────────────────────────────
        inline: list[ReviewThread] = []
        thread_nodes = await self._fetch_paginated_pr_connection_nodes(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "reviewThreads"),
            connection_name="reviewThreads",
            query=_GQL_PR_REVIEW_THREADS_PAGE,
        )
        for node in thread_nodes:
            thread_id = _clean_optional_str(node.get("id"))
            if thread_id is None:
                continue
            is_resolved = bool(node.get("isResolved"))
            is_outdated = bool(node.get("isOutdated"))
            comment_connection = _dig(node, "comments")
            all_comments = _parse_review_thread_comments(
                await self._fetch_paginated_review_thread_comment_nodes(
                    thread_id=thread_id,
                    first_page=comment_connection,
                )
            )
            latest_review_activity_at, latest_review_activity_source = (
                _latest_activity_from_thread_comments(
                    all_comments,
                    current_at=latest_review_activity_at,
                    current_source=latest_review_activity_source,
                )
            )
            if is_resolved or is_outdated:
                continue
            comments = tuple(comment for comment in all_comments if not comment.viewer_did_author)
            if not comments:
                continue
            first_comment = comments[0] if comments else None
            body = (first_comment.body if first_comment is not None else "")[:400]
            author = first_comment.author if first_comment is not None else None
            inline.append(
                ReviewThread(
                    thread_id=thread_id,
                    path=node.get("path"),
                    line=node.get("line"),
                    body_excerpt=body,
                    author=author,
                    is_resolved=is_resolved,
                    comments=comments,
                    url=first_comment.url if first_comment is not None else None,
                    is_outdated=is_outdated,
                )
            )

        # ── Review-level (outside-diff) comments ───────────────────────
        # A "review" is a top-level object that may or may not carry a
        # body. Non-empty bodies remain agent-triage feedback; the separate
        # blocking view uses only effective latest CHANGES_REQUESTED state.
        review_nodes = await self._fetch_paginated_pr_connection_nodes(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "reviews"),
            connection_name="reviews",
            query=_GQL_PR_REVIEWS_PAGE,
        )
        fetched_reviews = [
            _parse_fetched_review(node, fetch_index=index)
            for index, node in enumerate(review_nodes)
        ]
        latest_review_activity_at, latest_review_activity_source = _latest_activity_from_reviews(
            fetched_reviews,
            current_at=latest_review_activity_at,
            current_source=latest_review_activity_source,
        )
        blocking_reviews = _effective_blocking_reviews(fetched_reviews)
        reviews: list[ReviewComment] = [
            fetched.comment
            for fetched in fetched_reviews
            if not fetched.viewer_did_author and fetched.has_body
        ]

        # ── Top-level PR comments ──────────────────────────────────────
        # Review bots sometimes report feedback as top-level issue comments
        # instead of review objects. AWF filters only comments authored by the
        # current token identity; all third-party text is external feedback for
        # the agent to triage rather than control-plane policy.
        issue_comment_nodes = await self._fetch_paginated_pr_connection_nodes(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "comments"),
            connection_name="comments",
            query=_GQL_PR_ISSUE_COMMENTS_PAGE,
        )
        for node in issue_comment_nodes:
            body = node.get("body") or ""
            if node.get("isMinimized") or node.get("viewerDidAuthor") or not body.strip():
                continue
            author = _dig(node, "author", "login")
            created_at = _parse_github_datetime(node.get("createdAt"))
            updated_at = _parse_github_datetime(node.get("updatedAt"))
            latest_review_activity_at, latest_review_activity_source = _newer_activity(
                current_at=latest_review_activity_at,
                current_source=latest_review_activity_source,
                candidate_at=updated_at or created_at,
                candidate_source="issue_comment",
            )
            reviews.append(
                ReviewComment(
                    comment_id=f"issue:{node['databaseId']}",
                    body_excerpt=body[:400],
                    author=author,
                    is_resolved=False,
                    body=body,
                    url=_clean_optional_str(node.get("url")),
                    created_at=created_at,
                    updated_at=updated_at,
                    source_kind="issue",
                    viewer_did_author=False,
                )
            )

        changed_paths = await self._fetch_changed_paths(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "files"),
        )
        quiet_anchor_at, quiet_anchor_source = _quiet_period_anchor(
            latest_external_review_activity_at=latest_review_activity_at,
            latest_external_review_activity_source=latest_review_activity_source,
            pr_created_at=_parse_github_datetime(pr.get("createdAt")),
            pr_updated_at=_parse_github_datetime(pr.get("updatedAt")),
            head_committed_at=_parse_github_datetime(
                _dig(pr, "commits", "nodes", 0, "commit", "committedDate")
            ),
        )

        return PRStatus(
            number=pr["number"],
            head_sha=pr["headRefOid"],
            mergeable=mergeable,
            check_state=check_state,
            unresolved_inline_threads=tuple(inline),
            unresolved_review_comments=tuple(reviews),
            base_behind_count=base_behind_count,
            merge_state_status=merge_state_status,
            ci_failures=(),  # populated by fetch_failing_check_logs if needed
            checks=checks,
            changed_paths=changed_paths,
            closed=bool(pr.get("closed")),
            merged=bool(pr.get("merged")),
            merge_commit_sha=_clean_optional_str(_dig(pr, "mergeCommit", "oid")),
            blocking_reviews=blocking_reviews,
            latest_external_review_activity_at=latest_review_activity_at,
            latest_external_review_activity_source=latest_review_activity_source,
            quiet_period_anchor_at=quiet_anchor_at,
            quiet_period_anchor_source=quiet_anchor_source,
        )

    async def _fetch_paginated_pr_connection_nodes(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        first_page: Any,
        connection_name: str,
        query: str,
    ) -> list[dict[str, Any]]:
        """Fetch all nodes from a paginated pull-request GraphQL connection."""
        nodes = _connection_nodes(first_page)
        cursor = _clean_optional_str(_dig(first_page, "pageInfo", "endCursor"))
        has_next = _dig(first_page, "pageInfo", "hasNextPage") is True
        while has_next and cursor is not None:
            payload = await self._graphql(
                query=query,
                variables={
                    "owner": repo.owner,
                    "repo": repo.name,
                    "number": pr_number,
                    "cursor": cursor,
                },
            )
            page = _dig(payload, "data", "repository", "pullRequest", connection_name)
            nodes.extend(_connection_nodes(page))
            cursor = _clean_optional_str(_dig(page, "pageInfo", "endCursor"))
            has_next = _dig(page, "pageInfo", "hasNextPage") is True
        return nodes

    async def _fetch_paginated_review_thread_comment_nodes(
        self,
        *,
        thread_id: str,
        first_page: Any,
    ) -> list[dict[str, Any]]:
        """Fetch all comment nodes for a review thread using cursor pagination."""
        nodes = _connection_nodes(first_page)
        cursor = _clean_optional_str(_dig(first_page, "pageInfo", "endCursor"))
        has_next = _dig(first_page, "pageInfo", "hasNextPage") is True
        while has_next and cursor is not None:
            payload = await self._graphql(
                query=_GQL_REVIEW_THREAD_COMMENTS_PAGE,
                variables={"threadId": thread_id, "cursor": cursor},
            )
            page = _dig(payload, "data", "node", "comments")
            nodes.extend(_connection_nodes(page))
            cursor = _clean_optional_str(_dig(page, "pageInfo", "endCursor"))
            has_next = _dig(page, "pageInfo", "hasNextPage") is True
        return nodes

    async def _fetch_changed_paths(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        first_page: Any,
    ) -> tuple[str, ...]:
        """Collect changed file paths for the PR across all file pages."""
        changed_path_items = _extract_pr_file_paths(first_page)
        files_page = first_page

        while _dig(files_page, "pageInfo", "hasNextPage") is True:
            cursor = _clean_optional_str(_dig(files_page, "pageInfo", "endCursor"))
            if cursor is None:
                raise GitHubClientError(
                    operation="fetch_pr_status",
                    returncode=0,
                    stderr=("GitHub PR files pageInfo.hasNextPage was true without an endCursor"),
                )
            payload = await self._graphql(
                query=_GQL_PR_FILES_PAGE,
                variables={
                    "owner": repo.owner,
                    "repo": repo.name,
                    "number": pr_number,
                    "cursor": cursor,
                },
            )
            next_page = _dig(payload, "data", "repository", "pullRequest", "files")
            if not isinstance(next_page, dict):
                raise GitHubClientError(
                    operation="fetch_pr_status",
                    returncode=0,
                    stderr="GitHub PR files pagination response did not include files",
                )
            files_page = next_page
            changed_path_items.extend(_extract_pr_file_paths(files_page))

        return tuple(changed_path_items)

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,  # noqa: ARG002 - kept for API consistency with other PR-scoped calls
        head_sha: str,
        log_tail_chars: int = 3000,
        pytest_fallback_commands: Sequence[str] = (),
    ) -> tuple[CheckFailure, ...]:
        """Fetch logs for failing/timed-out checks via ``gh run view``.

        The GraphQL PR query only surfaces an aggregate ``statusCheckRollup``
        state. For a ``ReportCiFailure`` action we also want the per-check
        log so the coding CLI has something concrete to fix. We list the
        workflow runs for the head SHA, find the failing ones, and grab
        their failed-step logs.
        """
        runs_raw = await self._gh_json(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo.slug(),
                "--commit",
                head_sha,
                "--json",
                "databaseId,name,conclusion,status",
                "--limit",
                "20",
            ],
            operation="list_runs_for_sha",
        )
        failures: list[CheckFailure] = []
        for run in runs_raw or []:
            conclusion = run.get("conclusion") or ""
            if conclusion.upper() not in {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
                continue
            database_id = run.get("databaseId")
            run_id = str(database_id) if database_id is not None else None
            log = (
                await self._run_gh(
                    [
                        "gh",
                        "run",
                        "view",
                        run_id,
                        "--repo",
                        repo.slug(),
                        "--log-failed",
                    ],
                    operation="view_run_log",
                    strict=False,  # logs may be purged; don't fail the monitor
                )
                if run_id is not None
                else None
            )
            run_name = run.get("name") or (f"run/{run_id}" if run_id is not None else "run/unknown")
            raw_log_text = log.stdout if log is not None else ""
            log_text = redact_ci_log(raw_log_text)
            evidence = extract_ci_failure_evidence(
                raw_log_text,
                check_name=run_name,
                pytest_fallback_commands=pytest_fallback_commands,
            )
            failures.append(
                CheckFailure(
                    name=run_name,
                    conclusion=conclusion.upper(),
                    log_excerpt=_tail(log_text, log_tail_chars),
                    run_id=run_id,
                    failing_commands=evidence.failing_commands,
                    test_node_ids=evidence.test_node_ids,
                    assertion_snippets=evidence.assertion_snippets,
                    error_summaries=evidence.error_summaries,
                    suggested_repro_commands=evidence.suggested_repro_commands,
                    evidence_warnings=evidence.evidence_warnings,
                )
            )
        return tuple(failures)

    async def rerun_failed_workflow_jobs(self, *, repo: RepoRef, run_id: str) -> None:
        """Rerun only failed jobs for a workflow run.

        Used by the PR monitor before involving a coding agent when the
        failure evidence points at GitHub/runner/package-download
        infrastructure rather than repository code.
        """

        result = await self._runner.run(
            [
                "gh",
                "run",
                "rerun",
                run_id,
                "--repo",
                repo.slug(),
                "--failed",
            ]
        )
        if not result.ok:
            raise GitHubClientError(
                operation="rerun_failed_workflow_jobs",
                returncode=result.returncode,
                stderr=result.stderr,
            )

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Resolve a GitHub review thread by node ID."""
        await self._graphql(
            query=_GQL_RESOLVE_THREAD,
            variables={"threadId": thread_id},
        )

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Post a top-level PR comment (not a reply to a thread)."""
        result = await self._runner.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--body",
                body,
            ],
        )
        if not result.ok:
            raise GitHubClientError(
                operation="gh pr comment",
                returncode=result.returncode,
                stderr=result.stderr,
            )

    async def create_issue(self, *, repo: RepoRef, title: str, body: str) -> str:
        """Open a tracking issue and return its URL.

        Used by the PR monitor to capture a follow-up ``defer`` verdict durably
        before resolving the review thread. A token missing the ``issues``
        scope surfaces here as a ``GitHubClientError``; the caller treats that
        as a capture failure and leaves the thread unresolved so the merge gate
        keeps blocking (fail safe).
        """
        result = await self._runner.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo.slug(),
                "--title",
                title,
                "--body",
                body,
            ],
        )
        if not result.ok:
            raise GitHubClientError(
                operation="gh issue create",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result.stdout.strip()

    async def create_pull_request(
        self,
        *,
        repo: RepoRef,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR for an existing ``head`` branch against ``base``.

        Both branches already exist on origin (no worktree push), so this is
        a plain ``gh pr create`` and returns the new PR URL printed on stdout.
        """
        result = await self._runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo.slug(),
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            ],
        )
        if not result.ok:
            raise GitHubClientError(
                operation="gh pr create",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        match = _PR_URL_PATTERN.search(result.stdout)
        if match is None:
            # gh exited 0 but printed status/warning text rather than a PR URL.
            # Returning that verbatim would persist a non-URL ``pr_url`` and the
            # subsequent PR-number extraction would yield ``None``, breaking the
            # monitor handoff — fail loudly with the structured error instead.
            raise GitHubClientError(
                operation="gh pr create (no URL in stdout)",
                returncode=0,
                stderr=f"unexpected gh output: {result.stdout.strip()[:500]}",
            )
        return match.group(0)

    async def fetch_repo_merge_methods(self, *, repo: RepoRef) -> tuple[str, ...]:
        """Return repository-level merge methods enabled for pull requests."""
        payload = await self._gh_json(
            ["gh", "api", f"repos/{repo.slug()}"],
            operation="gh api repo",
        )
        if not isinstance(payload, dict):
            raise GitHubClientError(
                operation="gh api repo",
                returncode=0,
                stderr="GitHub repository response was not a JSON object",
            )
        merge_flags = (
            ("allow_merge_commit", "merge"),
            ("allow_squash_merge", "squash"),
            ("allow_rebase_merge", "rebase"),
        )
        missing_flags = [flag for flag, _method in merge_flags if flag not in payload]
        if missing_flags:
            missing = ", ".join(missing_flags)
            raise GitHubClientError(
                operation="gh api repo",
                returncode=1,
                stderr=(
                    "GitHub repository response omitted merge method flags; "
                    f"API response may be temporarily unavailable, try again: {missing}"
                ),
            )
        methods: list[str] = []
        for flag, method in merge_flags:
            if payload.get(flag) is True:
                methods.append(method)
        return tuple(methods)

    async def fetch_branch_pull_request_allowed_merge_methods(
        self,
        *,
        repo: RepoRef,
        branch: str,
    ) -> tuple[str, ...] | None:
        """Return base-branch pull-request ruleset merge methods.

        ``None`` means the effective branch rules do not constrain merge
        method choice. An empty tuple means recognized pull_request rules
        conflict and no known method satisfies all of them.
        """
        encoded_branch = quote(branch, safe="")
        payload = await self._gh_json(
            [
                "gh",
                "api",
                f"repos/{repo.slug()}/rules/branches/{encoded_branch}",
                "--paginate",
                "--slurp",
            ],
            operation="gh api branch rules",
        )
        if payload is None:
            raise GitHubClientError(
                operation="gh api branch rules",
                returncode=0,
                stderr=(
                    "GitHub branch rules empty response despite --paginate --slurp; "
                    "API response may be temporarily unavailable, try again"
                ),
            )
        if not isinstance(payload, list):
            raise GitHubClientError(
                operation="gh api branch rules",
                returncode=0,
                stderr="GitHub branch rules response was not a JSON array",
            )

        rules = _flatten_branch_rules_pages(payload)
        constrained: set[str] | None = None
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "pull_request":
                continue
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                continue
            allowed = parameters.get("allowed_merge_methods")
            if not isinstance(allowed, list):
                continue
            if not allowed:
                constrained = set()
                break
            normalized = {method for method in allowed if method in {"merge", "squash", "rebase"}}
            if not normalized:
                # The rule lists only unknown/future method values. AWF cannot enforce them;
                # treat the rule as non-constraining rather than blocking every method.
                continue
            constrained = (
                normalized if constrained is None else constrained.intersection(normalized)
            )

        if constrained is None:
            return None
        return tuple(method for method in ("merge", "squash", "rebase") if method in constrained)

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Merge a pull request using the given method and return the merge commit SHA.

        The caller is responsible for resolving the effective merge method via
        ``fetch_repo_merge_methods`` and
        ``fetch_branch_pull_request_allowed_merge_methods`` before calling this.
        Method-specific rejections are classified by the merge loop to retry with
        an alternative or escalate to NotifyHuman.
        """
        args = ["gh", "pr", "merge", str(pr_number), "--repo", repo.slug(), f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation="gh pr merge",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        # Merge commit SHA via a follow-up query (merge output is free-form).
        sha_result = await self._runner.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--json",
                "mergeCommit",
                "--jq",
                ".mergeCommit.oid // empty",
            ],
        )
        if not sha_result.ok:
            # Merge succeeded; the SHA fetch is best-effort for logging.
            return ""
        return sha_result.stdout.strip()

    # ── Internals ──────────────────────────────────────────────────────────

    async def _graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL query/mutation via ``gh api graphql``.

        ``gh api graphql --raw-field query=… -F owner=… -F repo=… -F number=…``
        is the usual invocation. We pass the query via ``-f`` for strings
        and numeric vars via ``-F`` (numeric). Keeping variables typed at
        the call site avoids gh's quirky shell-escaping.
        """
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            flag = "-F" if isinstance(value, int) else "-f"
            args.extend([flag, f"{key}={value}"])
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation="gh api graphql",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(
                operation="gh api graphql (json parse)",
                returncode=0,
                stderr=f"{exc}; stdout was: {result.stdout[:400]}",
            ) from exc
        if payload.get("errors"):
            raise GitHubClientError(
                operation="gh api graphql",
                returncode=0,
                stderr=json.dumps(payload["errors"])[:1000],
            )
        return payload  # type: ignore[no-any-return]  # json.loads returns Any; the explicit dict type is the caller's contract

    async def _gh_json(self, args: list[str], *, operation: str) -> Any:
        """Run a GH CLI JSON command and decode the stdout body."""
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(
                operation=f"{operation} (json parse)",
                returncode=0,
                stderr=f"{exc}; stdout was: {result.stdout[:400]}",
            ) from exc

    async def _run_gh(self, args: list[str], *, operation: str, strict: bool) -> Any:
        """Execute a GH CLI command, optionally enforcing success."""
        result = await self._runner.run(args)
        if not result.ok and strict:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result


def _flatten_branch_rules_pages(payload: list[Any]) -> list[Any]:
    """Normalize ``gh api --paginate --slurp`` branch-rule pages."""
    if not all(isinstance(page, list) for page in payload):
        return payload
    return [rule for page in payload for rule in page]


# ── Tiny helpers kept private to avoid accidental imports ──────────────────


from awf.common.github_client_adoption import (  # noqa: E402
    _looks_like_missing_pr_error,
    _parse_branch_open_pull_request,
    _parse_pull_request_adoption_metadata,
)
from awf.common.github_client_parsing import (  # noqa: E402
    _clean_optional_str,
    _connection_nodes,
    _dig,
    _effective_blocking_reviews,
    _extract_pr_file_paths,
    _latest_activity_from_reviews,
    _latest_activity_from_thread_comments,
    _newer_activity,
    _parse_check_contexts,
    _parse_check_state,
    _parse_fetched_review,
    _parse_github_datetime,
    _parse_merge_state_status,
    _parse_mergeable,
    _parse_review_thread_comments,
    _quiet_period_anchor,
    _tail,
)
