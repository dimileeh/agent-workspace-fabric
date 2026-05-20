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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner
from awf.common.logging import get_logger
from awf.runtime.ci_failure_evidence import extract_ci_failure_evidence, redact_ci_log
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewComment,
    ReviewThread,
    ReviewThreadComment,
)

_log = get_logger(__name__)


class GitHubClientError(Exception):
    """Raised when ``gh`` or GraphQL returns a non-zero exit / error payload."""

    def __init__(self, *, operation: str, returncode: int, stderr: str) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{operation} failed (exit={returncode}): {stderr.strip() or '<no output>'}"
        )


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
    fetch_index: int
    viewer_did_author: bool
    has_body: bool
    counts_for_required_review: bool


# GraphQL: fetch PR state + review threads + review comments in one query.
# The changed-file list feeds merge policy, so it is paginated below whenever
# GitHub reports more than the first 100 paths.
_GQL_PR_STATE = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      headRefOid
      mergeable
      mergeStateStatus
      isDraft
      closed
      merged
      mergeCommit { oid }
      baseRef { name target { ... on Commit { oid } } }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    startedAt
                    completedAt
                    detailsUrl
                    checkSuite {
                      app {
                        slug
                        name
                      }
                      creator {
                        login
                      }
                    }
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                    creator {
                      login
                    }
                  }
                }
                pageInfo { hasNextPage }
              }
            }
          }
        }
      }
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              databaseId
              bodyText
              author { login }
              viewerDidAuthor
              createdAt
              url
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
      reviews(first: 100) {
        nodes {
          databaseId
          body
          state
          submittedAt
          commit { oid }
          url
          author { login }
          authorCanPushToRepository
          viewerDidAuthor
        }
        pageInfo { hasNextPage endCursor }
      }
      comments(first: 100) {
        nodes {
          databaseId
          body
          isMinimized
          viewerDidAuthor
          createdAt
          updatedAt
          url
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
      files(first: 100) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_FILES_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) {
        nodes { path }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_REVIEW_THREADS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              databaseId
              bodyText
              author { login }
              viewerDidAuthor
              createdAt
              url
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_REVIEW_THREAD_COMMENTS_PAGE = """
query($threadId: ID!, $cursor: String!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $cursor) {
        nodes {
          databaseId
          bodyText
          author { login }
          viewerDidAuthor
          createdAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_REVIEWS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $cursor) {
        nodes {
          databaseId
          body
          state
          submittedAt
          commit { oid }
          url
          author { login }
          authorCanPushToRepository
          viewerDidAuthor
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


_GQL_PR_ISSUE_COMMENTS_PAGE = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        nodes {
          databaseId
          body
          isMinimized
          viewerDidAuthor
          createdAt
          updatedAt
          url
          author { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


# GraphQL: mutation to resolve a review thread by node ID.
_GQL_RESOLVE_THREAD = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


@dataclass(frozen=True)
class RepoRef:
    """Owner + repo name parsed out of URLs like
    ``git@github.com:org/repo.git`` or ``https://github.com/org/repo``."""

    owner: str
    name: str

    @classmethod
    def from_url(cls, repo_url: str) -> RepoRef:
        value = repo_url.strip()
        slug_match = re.fullmatch(r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?", value)
        if slug_match and "github.com" not in value and ":" not in value:
            return cls(owner=slug_match.group(1), name=slug_match.group(2))

        # SSH form: ``git@github.com:owner/repo.git``.
        ssh_match = re.fullmatch(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?", value)
        if ssh_match:
            return cls(owner=ssh_match.group(1), name=ssh_match.group(2))

        parsed = urlsplit(value)
        http_github_url = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.lower() == "github.com"
        )
        ssh_github_url = (
            parsed.scheme == "ssh"
            and parsed.hostname is not None
            and parsed.hostname.lower() == "github.com"
            and (parsed.username is None or parsed.username.lower() == "git")
        )
        if http_github_url or ssh_github_url:
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0] and parts[1]:
                name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
                if name:
                    return cls(owner=parts[0], name=name)

            raise ValueError(f"Cannot parse GitHub repo from URL: {repo_url!r}")

        raise ValueError(f"Cannot parse GitHub repo from URL: {repo_url!r}")

    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def https_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"

    def ssh_url(self) -> str:
        return f"git@github.com:{self.owner}/{self.name}.git"

    def clone_url_like(self, repo_url: str) -> str:
        stripped = repo_url.strip()
        if stripped.startswith("git@github.com:") or stripped.startswith("ssh://git@github.com/"):
            return self.ssh_url()

        parsed = urlsplit(stripped)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.hostname.lower() == "github.com"
        ):
            userinfo, sep, _host = parsed.netloc.rpartition("@")
            if sep and userinfo:
                return f"https://{userinfo}@github.com/{self.owner}/{self.name}.git"
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
    head_sha: str | None = None


_PR_ADOPTION_VIEW_JSON_FIELDS = (
    "number,headRefName,headRepository,isCrossRepository,baseRefName,"
    "headRefOid,baseRefOid,state,isDraft,author,url,title"
)
_BRANCH_OPEN_PR_LIST_JSON_FIELDS = "number,url,headRefName,headRefOid"


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
    return [
        _parse_branch_open_pull_request(item, repo=repo, branch_name=stripped_branch)
        for item in payload
    ]


class BranchOpenPullRequestResolver:
    """Resolve open PRs for a branch using the GitHub CLI."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        self._runner = runner

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[BranchOpenPullRequest]:
        try:
            repo = RepoRef.from_url(repo_url)
        except ValueError as exc:
            _log.warning(
                "github.open_pr_lookup_skipped_invalid_repo_url",
                repo_url=redact_audit_text(repo_url),
                branch_name=branch_name,
                base_branch=base_branch,
                error=redact_audit_text(str(exc)),
            )
            return []
        return await list_open_pull_requests_for_branch(
            runner=self._runner,
            repo=repo,
            branch_name=branch_name,
            base_branch=base_branch,
        )


def _parse_pull_request_adoption_metadata(
    payload: dict[str, Any],
    *,
    repo: RepoRef,
    pr_number: int,
) -> PullRequestAdoptionMetadata:
    try:
        number = int(payload["number"])
        head_ref = str(payload["headRefName"])
        base_ref = str(payload["baseRefName"])
        state = str(payload["state"])
        is_draft = bool(payload.get("isDraft", False))
        url = str(payload["url"])
        title = str(payload.get("title") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"gh pr view payload missing required adoption field: {exc}",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        ) from exc

    if number != pr_number:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=(f"gh pr view returned PR #{number}, expected #{pr_number} for {repo.slug()}"),
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )
    if not head_ref.strip():
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message="PR has no headRefName; cannot check out the PR branch.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )
    if not base_ref.strip():
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message="PR has no baseRefName; cannot record the merge target.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )

    closed = state == "CLOSED"
    merged = state == "MERGED"

    author_obj = payload.get("author")
    author = author_obj.get("login") if isinstance(author_obj, dict) else None
    head_repo_slug = _head_repo_slug_from_adoption_payload(
        payload,
        repo=repo,
        pr_number=pr_number,
    )
    head_sha = _required_nonempty_str(
        payload.get("headRefOid"),
        field_name="headRefOid",
        repo=repo,
        pr_number=pr_number,
        message="PR has no headRefOid; cannot adopt the PR monitor without a head commit.",
    )
    base_sha = _required_nonempty_str(
        payload.get("baseRefOid"),
        field_name="baseRefOid",
        repo=repo,
        pr_number=pr_number,
        message="PR has no baseRefOid; cannot adopt the PR monitor without a base commit.",
    )
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref=head_ref,
        head_repo_slug=head_repo_slug,
        base_ref=base_ref,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        is_draft=is_draft,
        closed=closed,
        merged=merged,
        author=author,
        url=url,
        title=title,
    )


def _parse_branch_open_pull_request(
    payload: object,
    *,
    repo: RepoRef,
    branch_name: str,
) -> BranchOpenPullRequest:
    if not isinstance(payload, dict):
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list item is not an object.",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        )
    try:
        number = int(payload["number"])
        raw_url = payload["url"]
        if not isinstance(raw_url, str):
            raise TypeError("url")
        url = raw_url.strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"gh pr list payload missing required field: {exc}",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        ) from exc
    if number <= 0 or not url:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list payload has invalid number or URL.",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        )

    head_ref = _optional_nonempty_str(payload.get("headRefName")) or branch_name
    head_sha = _optional_nonempty_str(payload.get("headRefOid"))
    return BranchOpenPullRequest(
        url=url,
        number=number,
        head_ref=head_ref,
        head_sha=head_sha,
    )


def _head_repo_slug_from_adoption_payload(
    payload: dict[str, Any],
    *,
    repo: RepoRef,
    pr_number: int,
) -> str:
    head_repo = payload.get("headRepository")
    if isinstance(head_repo, dict):
        name_with_owner = head_repo.get("nameWithOwner")
        if isinstance(name_with_owner, str) and name_with_owner.strip():
            try:
                return RepoRef.from_url(name_with_owner).slug()
            except ValueError as exc:
                raise PullRequestMetadataError(
                    reason_code="PR_METADATA_INVALID",
                    message="PR headRepository.nameWithOwner is not a valid GitHub repository.",
                    detail={
                        "repo_slug": repo.slug(),
                        "pr_number": pr_number,
                        "field": "headRepository.nameWithOwner",
                    },
                ) from exc

    if not bool(payload.get("isCrossRepository", False)):
        return repo.slug()

    raise PullRequestMetadataError(
        reason_code="PR_METADATA_INVALID",
        message="Fork PR has no headRepository identity; cannot update the PR head.",
        detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": "headRepository"},
    )


def _required_nonempty_str(
    value: object,
    *,
    field_name: str,
    repo: RepoRef,
    pr_number: int,
    message: str,
) -> str:
    if not isinstance(value, str):
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"{message} Missing field: {field_name}.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": field_name},
        )
    stripped = value.strip()
    if not stripped:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"{message} Blank field: {field_name}.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": field_name},
        )
    return stripped


def _optional_nonempty_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_like_missing_pr_error(stderr: str) -> bool:
    lower = stderr.lower()
    return (
        "could not resolve to a pullrequest" in lower
        or "not found" in lower
        or "no pull requests found" in lower
    )


class GitHubClient:
    """Stateless façade over ``gh`` CLI + GraphQL. Re-entrant."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        self._runner = runner

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
            if is_resolved or is_outdated:
                continue
            comments = _parse_review_thread_comments(
                await self._fetch_paginated_review_thread_comment_nodes(
                    thread_id=thread_id,
                    first_page=_dig(node, "comments"),
                )
            )
            comments = tuple(comment for comment in comments if not comment.viewer_did_author)
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
            reviews.append(
                ReviewComment(
                    comment_id=f"issue:{node['databaseId']}",
                    body_excerpt=body[:400],
                    author=author,
                    is_resolved=False,
                    body=body,
                    url=_clean_optional_str(node.get("url")),
                    created_at=_parse_github_datetime(node.get("createdAt")),
                    source_kind="issue",
                    viewer_did_author=False,
                )
            )

        changed_paths = await self._fetch_changed_paths(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "files"),
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

    async def merge_pr(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        method: str = "squash",
        delete_branch: bool = True,
    ) -> str:
        """Squash-merge (or merge / rebase) and return the merge commit SHA.

        Branch protection may reject the merge — that's how the runner
        decides to fall back to NotifyHuman.
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
        result = await self._runner.run(args)
        if not result.ok:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    async def _run_gh(self, args: list[str], *, operation: str, strict: bool) -> Any:
        result = await self._runner.run(args)
        if not result.ok and strict:
            raise GitHubClientError(
                operation=operation,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result


# ── Tiny helpers kept private to avoid accidental imports ──────────────────


def _dig(obj: Any, *keys: Any) -> Any:
    """Like ``obj.get(k1, {}).get(k2, {}) ...`` but survives lists + None."""
    cur = obj
    for k in keys:
        if cur is None:
            return None
        if isinstance(k, int):
            if not isinstance(cur, list) or k >= len(cur):
                return None
            cur = cur[k]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
    return cur


def _parse_check_state(value: str) -> CheckState:
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
                url=_clean_optional_str(node.get("url")),
            )
        )
    return tuple(comments)


def _parse_fetched_review(node: dict[str, Any], *, fetch_index: int) -> _FetchedReview:
    raw_body = node.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    body_excerpt = body[:400] if body.strip() else ""
    database_id = node.get("databaseId")
    comment_id = str(database_id if database_id is not None else f"missing:{fetch_index}")
    author = _clean_optional_str(_dig(node, "author", "login"))
    submitted_at = _parse_github_datetime(node.get("submittedAt"))
    comment = ReviewComment(
        comment_id=comment_id,
        body_excerpt=body_excerpt,
        author=author,
        is_resolved=False,
        body=body,
        url=_clean_optional_str(node.get("url")),
        created_at=submitted_at,
        state=(node.get("state") or "").upper(),
        source_kind="review",
        viewer_did_author=bool(node.get("viewerDidAuthor")),
    )
    return _FetchedReview(
        comment=comment,
        reviewer_key=_reviewer_effective_state_key(node, fetch_index=fetch_index),
        submitted_at=submitted_at,
        fetch_index=fetch_index,
        viewer_did_author=comment.viewer_did_author,
        has_body=bool(body.strip()),
        counts_for_required_review=_review_counts_for_required_review(node),
    )


def _reviewer_effective_state_key(node: dict[str, Any], *, fetch_index: int) -> str:
    author = _clean_optional_str(_dig(node, "author", "login"))
    if author is not None:
        return f"author:{author.lower()}"
    database_id = node.get("databaseId")
    if database_id is not None:
        return f"review:{database_id}"
    return f"review-fetch-index:{fetch_index}"


def _review_counts_for_required_review(node: dict[str, Any]) -> bool:
    # Real GraphQL payloads include this Boolean. Older/fake payloads are treated
    # as counting to preserve conservative merge-gate behavior.
    # This is a push-access heuristic, not a complete GitHub branch-protection
    # required-reviewer model; read-only code-owner blocks still surface through
    # GitHub's mergeable state.
    return node.get("authorCanPushToRepository") is not False


def _effective_blocking_reviews(
    fetched_reviews: Sequence[_FetchedReview],
) -> tuple[ReviewComment, ...]:
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
    upper = (value or "").upper()
    if upper == "MERGEABLE":
        return MergeableState.MERGEABLE
    if upper == "CONFLICTING":
        return MergeableState.CONFLICTING
    return MergeableState.UNKNOWN


def _parse_merge_state_status(value: Any) -> MergeStateStatus:
    """GraphQL returns one of: CLEAN / BEHIND / DIRTY / BLOCKED / HAS_HOOKS
    / UNSTABLE / UNKNOWN. Default to UNKNOWN for anything we don't
    recognise — decide() treats UNKNOWN as "wait, don't act"."""
    upper = (value or "").upper()
    try:
        return MergeStateStatus(upper)
    except ValueError:
        return MergeStateStatus.UNKNOWN


def _tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return "…[truncated]…\n" + text[-n:]
