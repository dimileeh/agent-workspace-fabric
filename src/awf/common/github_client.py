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

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.forge_errors import ForgeClientError
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.common.github_client_adoption import (
    BranchOpenPullRequestResolver as BranchOpenPullRequestResolver,
)
from awf.common.github_client_adoption import (
    fetch_pull_request_adoption_metadata as fetch_pull_request_adoption_metadata,
)
from awf.common.github_client_adoption import (
    list_open_pull_requests_for_branch as list_open_pull_requests_for_branch,
)
from awf.common.github_client_adoption import (
    parse_github_pull_request_url as parse_github_pull_request_url,
)
from awf.common.github_client_ref import RepoRef as RepoRef
from awf.common.github_graphql import (
    _GQL_PR_FILES_PAGE,
    _GQL_PR_ISSUE_COMMENTS_PAGE,
    _GQL_PR_LIFECYCLE,
    _GQL_PR_REVIEW_THREADS_PAGE,
    _GQL_PR_REVIEWS_PAGE,
    _GQL_PR_STATE,
    _GQL_RESOLVE_THREAD,
    _GQL_REVIEW_THREAD_COMMENTS_PAGE,
)
from awf.common.github_retry import (
    GITHUB_TRANSPORT_MAX_ATTEMPTS,  # noqa: F401
    GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS,  # noqa: F401
    PullRequestCreateTransportOutcome,
    Reconciler,
    RetryPolicy,
)
from awf.common.github_transient import (
    GitHubErrorDisposition,
    github_error_disposition,
)
from awf.common.github_transport import execute_gh_with_retry as _execute_gh_with_retry
from awf.common.logging import get_logger
from awf.runtime.ci_failure_evidence import extract_ci_failure_evidence, redact_ci_log
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckFailureLogResult,
    CheckTiming,
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


def _branch_open_pr_metadata(pr: BranchOpenPullRequest) -> dict[str, object]:
    metadata: dict[str, object] = {
        "number": pr.number,
        "url": pr.url,
        "head_ref": pr.head_ref,
        "head_repo_slug": pr.head_repo_slug,
    }
    if pr.head_sha is not None:
        metadata["head_sha"] = pr.head_sha
    return metadata


# GitHub shells ``gh`` and carries no HTTP status, so it has no native per-fault
# reason code. The shared ``ForgeClientError`` contract requires a stable
# ``reason_code`` for every forge fault, so GitHub faults default to this code (the
# PR monitor classifies transient-vs-deterministic from stderr markers, not this
# code). Defined here, in a client file, so the reason catalog picks it up.
GITHUB_API_ERROR = "GITHUB_API_ERROR"


def _transient_graphql_payload_error(result: CommandResult) -> str | None:
    """Flag an exit-0 ``gh api graphql`` body that carries a transient errors array.

    ``gh api graphql`` exits 0 for an HTTP-200 response even when the body holds a
    GraphQL ``errors`` array (gh only surfaces errors as a non-zero exit for
    HTTP >= 400). A transient server-side blip ("something went wrong", rate-limit
    guidance) therefore returns with a 0 exit and, without this hook, the
    ``payload.get("errors")`` raise in ``_graphql`` would bypass the transport's
    in-cycle retry entirely. Returning the errors text lets the transport retry it
    under the caller's policy. Only *transient* payloads are surfaced here — a
    permanent/schema error still falls through to the ``_graphql`` raise unchanged,
    so its fast-fail behavior is preserved.
    """

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        # A malformed body is handled by _graphql's own JSON decode + raise; the
        # transport must not misread it as a retryable payload error.
        return None
    if not isinstance(payload, dict):
        return None
    errors = payload.get("errors")
    if not errors:
        return None
    errors_text = json.dumps(errors)
    disposition = github_error_disposition(operation="gh api graphql", stderr=errors_text)
    if disposition == GitHubErrorDisposition.TRANSIENT:
        return errors_text[:1000]
    return None


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


class GitHubClient:
    """Stateless façade over ``gh`` CLI + GraphQL. Re-entrant."""

    def __init__(
        self,
        runner: AsyncCommandRunner,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Store the shared command runner for all GitHub operations."""
        self._runner = runner
        self._sleep = sleep or asyncio.sleep
        self._last_pr_create_outcome: PullRequestCreateTransportOutcome | None = None

    @property
    def last_pr_create_outcome(self) -> PullRequestCreateTransportOutcome | None:
        """Transport audit metadata from the most recent ``create_pull_request`` call."""

        return self._last_pr_create_outcome

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

    async def fetch_pull_request_lifecycle(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
    ) -> PullRequestLifecycle:
        """Return a PR's lifecycle using a minimal retrying read."""
        payload = await self._graphql(
            query=_GQL_PR_LIFECYCLE,
            variables={"owner": repo.owner, "repo": repo.name, "number": pr_number},
            retry_policy=RetryPolicy.READ,
        )
        pr = payload["data"]["repository"]["pullRequest"]
        if pr is None:
            return PullRequestLifecycle.missing
        if pr["merged"]:
            return PullRequestLifecycle.merged
        if pr["closed"]:
            return PullRequestLifecycle.closed
        return PullRequestLifecycle.open

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
        retry: bool = True,
    ) -> PRStatus:
        """Single GraphQL round-trip; assembles a ``PRStatus``.

        ``base_behind_count`` is computed by the caller via local git
        (``git rev-list --count HEAD..origin/<base>``). GitHub's
        ``mergeable`` field tells us *whether* there's a conflict but not
        the count of commits behind, so we pass it in.

        ``retry`` defaults to ``True`` for ordinary polling, where a transient
        blip recovers in-cycle (allow-by-default). The pre-merge recheck passes
        ``retry=False`` so a single failure raises promptly and the merge critical
        section classifies it (transient -> re-poll next cycle; unknown -> fail)
        rather than holding the merge lock across in-cycle backoffs.
        """
        # One policy for the whole read, including every pagination follow-up:
        # ``retry=False`` (pre-merge recheck) must fail fast on page 2+ too, or a
        # transient blip on a later page would run transport backoffs while the
        # merge critical section is meant to fail fast.
        retry_policy = RetryPolicy.READ if retry else RetryPolicy.NEVER
        payload = await self._graphql(
            query=_GQL_PR_STATE,
            variables={
                "owner": repo.owner,
                "repo": repo.name,
                "number": pr_number,
            },
            retry_policy=retry_policy,
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
        # Authoritative "no checks observed": GitHub returns no rollup at all for
        # a commit with no CI, and a present-but-empty rollup reports
        # ``contexts.totalCount == 0``. Read the authoritative count, NOT
        # ``len(checks)`` (which a 100-context page cap could understate).
        no_checks_observed = rollup is None or _dig(rollup, "contexts", "totalCount") == 0
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
        # Threads the forge marks OUTDATED but still unresolved. Kept out of the
        # actionable ``inline`` feed (they are non-blocking for merge) but
        # surfaced separately so the monitor can resolve the ones it addressed
        # before they linger on a merged PR (#473).
        outdated_unresolved: list[ReviewThread] = []
        thread_nodes = await self._fetch_paginated_pr_connection_nodes(
            repo=repo,
            pr_number=pr_number,
            first_page=_dig(pr, "reviewThreads"),
            connection_name="reviewThreads",
            query=_GQL_PR_REVIEW_THREADS_PAGE,
            retry_policy=retry_policy,
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
                    retry_policy=retry_policy,
                )
            )
            latest_review_activity_at, latest_review_activity_source = (
                _latest_activity_from_thread_comments(
                    all_comments,
                    current_at=latest_review_activity_at,
                    current_source=latest_review_activity_source,
                )
            )
            # Resolved threads are dropped from both feeds — there is nothing
            # left to action or to resolve. An OUTDATED-but-unresolved thread is
            # still dropped from the actionable ``inline`` feed (non-blocking for
            # merge) but routed to ``outdated_unresolved`` so the monitor can
            # resolve it if it already addressed the underlying feedback (#473).
            if is_resolved:
                continue
            comments = tuple(comment for comment in all_comments if not comment.viewer_did_author)
            if not comments:
                continue
            first_comment = comments[0] if comments else None
            body = (first_comment.body if first_comment is not None else "")[:400]
            author = first_comment.author if first_comment is not None else None
            thread = ReviewThread(
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
            (outdated_unresolved if is_outdated else inline).append(thread)

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
            retry_policy=retry_policy,
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
            retry_policy=retry_policy,
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
            retry_policy=retry_policy,
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
            head_ref=_clean_optional_str(pr.get("headRefName")),
            base_ref=_clean_optional_str(_dig(pr, "baseRef", "name")),
            head_sha=pr["headRefOid"],
            mergeable=mergeable,
            check_state=check_state,
            unresolved_inline_threads=tuple(inline),
            unresolved_review_comments=tuple(reviews),
            base_behind_count=base_behind_count,
            merge_state_status=merge_state_status,
            ci_failures=(),  # populated by fetch_failing_check_logs if needed
            checks=checks,
            no_checks_observed=no_checks_observed,
            changed_paths=changed_paths,
            closed=bool(pr.get("closed")),
            merged=bool(pr.get("merged")),
            merge_commit_sha=_clean_optional_str(_dig(pr, "mergeCommit", "oid")),
            blocking_reviews=blocking_reviews,
            latest_external_review_activity_at=latest_review_activity_at,
            latest_external_review_activity_source=latest_review_activity_source,
            quiet_period_anchor_at=quiet_anchor_at,
            quiet_period_anchor_source=quiet_anchor_source,
            outdated_unresolved_inline_threads=tuple(outdated_unresolved),
        )

    async def _fetch_paginated_pr_connection_nodes(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        first_page: Any,
        connection_name: str,
        query: str,
        retry_policy: RetryPolicy = RetryPolicy.READ,
    ) -> list[dict[str, Any]]:
        """Fetch all nodes from a paginated pull-request GraphQL connection.

        ``retry_policy`` is threaded through so a ``retry=False`` caller (the
        pre-merge recheck) fails fast on page 2+ as well as on the first page.
        """
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
                retry_policy=retry_policy,
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
        retry_policy: RetryPolicy = RetryPolicy.READ,
    ) -> list[dict[str, Any]]:
        """Fetch all comment nodes for a review thread using cursor pagination.

        ``retry_policy`` is threaded through so a ``retry=False`` caller (the
        pre-merge recheck) fails fast on page 2+ as well as on the first page.
        """
        nodes = _connection_nodes(first_page)
        cursor = _clean_optional_str(_dig(first_page, "pageInfo", "endCursor"))
        has_next = _dig(first_page, "pageInfo", "hasNextPage") is True
        while has_next and cursor is not None:
            payload = await self._graphql(
                query=_GQL_REVIEW_THREAD_COMMENTS_PAGE,
                variables={"threadId": thread_id, "cursor": cursor},
                retry_policy=retry_policy,
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
        retry_policy: RetryPolicy = RetryPolicy.READ,
    ) -> tuple[str, ...]:
        """Collect changed file paths for the PR across all file pages.

        ``retry_policy`` is threaded through so a ``retry=False`` caller (the
        pre-merge recheck) fails fast on page 2+ as well as on the first page.
        """
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
                retry_policy=retry_policy,
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
        rollup_checks: Sequence[CheckTiming] = (),
    ) -> CheckFailureLogResult:
        """Fetch logs for failing/timed-out checks via ``gh run view``.

        The GraphQL PR query only surfaces an aggregate ``statusCheckRollup``
        state. For a ``ReportCiFailure`` action we also want the per-check
        log so the coding CLI has something concrete to fix. We list the
        workflow runs for the head SHA, find the failing ones, and grab their
        failed-step logs. If ``gh run list --commit`` misses a failed Actions
        run that the PR rollup already exposed, fall back to the check's
        ``detailsUrl`` run id. If a failed non-Actions rollup check cannot be
        mapped to an Actions run, report its context name without log text
        instead of returning an empty failure snapshot.
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
        seen_run_ids: set[str] = set()
        runs_in_progress = False
        status_by_run: dict[str, str] = {}
        for run in runs_raw or []:
            database_id = run.get("databaseId")
            if database_id is not None:
                status = str(run.get("status") or "").lower()
                status_by_run[str(database_id)] = status

        async def _append_failure(
            *,
            run_id: str | None,
            run_name: str,
            conclusion: str,
        ) -> None:
            """Fetch failed-step logs and append one structured ``CheckFailure``."""
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
                    conclusion=conclusion,
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

        def _append_rollup_failure_without_log(
            *, run_name: str, conclusion: str, details_url: str | None
        ) -> None:
            """Record a rollup check failure when no job log could be fetched."""
            evidence_warnings = (
                (f"External check details URL: {redact_ci_log(details_url)}",)
                if details_url
                else ()
            )
            failures.append(
                CheckFailure(
                    name=run_name,
                    conclusion=conclusion,
                    log_excerpt="",
                    run_id=None,
                    evidence_warnings=evidence_warnings,
                )
            )

        for run in runs_raw or []:
            conclusion = run.get("conclusion") or ""
            conclusion_upper = conclusion.upper()
            if conclusion_upper not in _FAILED_CHECK_CONCLUSIONS:
                continue
            database_id = run.get("databaseId")
            run_id = str(database_id) if database_id is not None else None
            if run_id is not None:
                seen_run_ids.add(run_id)
                run_status = status_by_run.get(run_id)
                if run_status and run_status != "completed":
                    runs_in_progress = True
                    continue
            run_name = run.get("name") or (f"run/{run_id}" if run_id is not None else "run/unknown")
            await _append_failure(
                run_id=run_id,
                run_name=run_name,
                conclusion=conclusion_upper,
            )
        for check in rollup_checks:
            conclusion = _rollup_check_failure_conclusion(check)
            if conclusion is None:
                continue
            run_id = _actions_run_id_from_details_url(check.details_url)
            if run_id is None:
                if _rollup_check_has_external_details_url(
                    check
                ) or not _rollup_check_is_explicit_github_actions(check):
                    _append_rollup_failure_without_log(
                        run_name=check.name,
                        conclusion=conclusion,
                        details_url=check.details_url,
                    )
                continue
            if not _rollup_check_is_github_actions(check) or run_id in seen_run_ids:
                continue
            run_status = status_by_run.get(run_id)
            if run_status and run_status != "completed":
                runs_in_progress = True
                seen_run_ids.add(run_id)
                continue
            seen_run_ids.add(run_id)
            await _append_failure(
                run_id=run_id,
                run_name=check.name,
                conclusion=conclusion,
            )
        if not failures and any(
            status != "completed" for status in status_by_run.values() if status
        ):
            runs_in_progress = True
        return CheckFailureLogResult(
            failures=tuple(failures),
            runs_in_progress=runs_in_progress,
        )

    async def rerun_failed_workflow_jobs(self, *, repo: RepoRef, run_id: str) -> None:
        """Rerun only failed jobs for a workflow run.

        Used by the PR monitor before involving a coding agent when the
        failure evidence points at GitHub/runner/package-download
        infrastructure rather than repository code.
        """

        # NEVER-retry: the centralized transport raises a ``GitHubClientError`` on a
        # non-zero exit, so no separate ``result.ok`` re-check is needed here.
        await self._run_gh_command(
            [
                "gh",
                "run",
                "rerun",
                run_id,
                "--repo",
                repo.slug(),
                "--failed",
            ],
            operation="rerun_failed_workflow_jobs",
            retry_policy=RetryPolicy.NEVER,
        )

    async def resolve_thread(self, *, thread_id: str) -> None:
        """Resolve a GitHub review thread by node ID."""
        await self._graphql(
            query=_GQL_RESOLVE_THREAD,
            variables={"threadId": thread_id},
            retry_policy=RetryPolicy.NEVER,
        )

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Post a top-level PR comment (not a reply to a thread)."""
        await self._run_gh_command(
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
            operation="gh pr comment",
            retry_policy=RetryPolicy.NEVER,
        )

    async def fetch_pull_request_body(self, *, repo: RepoRef, pr_number: int) -> str:
        """Return a pull request's current description body.

        Used by release-PR sync to reconcile *only* AWF's managed merge-policy
        section into a reused PR — preserving any human-authored release notes,
        checklists, or context rather than overwriting the whole description. A
        read, so ``READ`` retry policy lets a transient blip recover. ``gh``'s
        ``--jq`` emits the raw body plus one trailing newline; strip only that so
        the body's internal formatting is preserved faithfully.

        A PR opened without a description has a JSON ``null`` body, which a bare
        ``.body`` filter renders as the literal text ``null``. That sentinel would
        reach release-PR reconciliation as human-authored content and be written
        back as a description reading "null", so the ``// ""`` alternative maps it
        to an empty body at the source (same shape as the ``// empty`` fallback in
        the post-merge SHA read). Filtering here rather than normalizing the
        rendered output also keeps a description whose literal text *is* ``null``
        intact — the two are indistinguishable once ``gh`` has rendered them.
        """
        result = await self._run_gh_command(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--json",
                "body",
                "--jq",
                '.body // ""',
            ],
            operation="gh pr view body",
            retry_policy=RetryPolicy.READ,
        )
        return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout

    async def update_pull_request_body(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        """Overwrite an existing PR's description.

        Used by release-PR sync to reconcile a reused PR's advertised merge
        policy: a PR opened earlier under one ``auto_merge`` resolution is reused
        without re-running the creator that applies the body, so its description
        can contradict the policy the attached monitor now enforces. Setting the
        body is idempotent, so ``NEVER`` retry keeps a transient blip from issuing
        a duplicate edit; the caller treats a failure as a reason-coded error.
        """
        await self._run_gh_command(
            [
                "gh",
                "pr",
                "edit",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--body",
                body,
            ],
            operation="gh pr edit body",
            retry_policy=RetryPolicy.NEVER,
        )

    async def create_issue(self, *, repo: RepoRef, title: str, body: str) -> str:
        """Open a tracking issue and return its URL.

        Used by the PR monitor to capture a follow-up ``defer`` verdict durably
        before resolving the review thread. A token missing the ``issues``
        scope surfaces here as a ``GitHubClientError``; the caller treats that
        as a capture failure and leaves the thread unresolved so the merge gate
        keeps blocking (fail safe).
        """
        result = await self._run_gh_command(
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
            operation="gh issue create",
            retry_policy=RetryPolicy.NEVER,
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
        transient_max_attempts: int | None = None,
    ) -> str:
        """Open a PR for an existing ``head`` branch against ``base``.

        ``transient_max_attempts`` bounds the in-transport reconcile+retry budget
        (the caller derives it from ``pr_create_transient_max_retries``); ``None``
        uses the transport default.

        Both branches already exist on origin (no worktree push), so this is
        a plain ``gh pr create`` and returns the new PR URL printed on stdout.
        """
        failures: list[dict[str, object]] = []
        reconcile_lookups: list[dict[str, object]] = []
        reconciled_match: BranchOpenPullRequest | None = None
        duplicate_lookup_failed = False

        async def _reconcile_create_pr() -> str | None:
            nonlocal reconciled_match, duplicate_lookup_failed
            try:
                candidates = await list_open_pull_requests_for_branch(
                    runner=self._runner,
                    repo=repo,
                    branch_name=head,
                    base_branch=base,
                    # Reconcile recheck after a create failure: retry a transient
                    # lookup so a blip doesn't wedge dedupe into a redundant create.
                    retry_policy=RetryPolicy.READ,
                )
            except (PullRequestMetadataError, GitHubClientError) as exc:
                duplicate_lookup_failed = True
                if isinstance(exc, PullRequestMetadataError):
                    reconcile_lookups.append(
                        {
                            "status": "failed",
                            "reason_code": exc.reason_code,
                            "message": exc.message[:500],
                        }
                    )
                else:
                    reconcile_lookups.append(
                        {
                            "status": "failed",
                            "reason_code": "OPEN_PR_LOOKUP_FAILED",
                            "operation": exc.operation,
                            "returncode": exc.returncode,
                            "error_message": exc.stderr[:500],
                        }
                    )
                return None

            repo_slug = repo.slug().lower()
            same_repo = [
                candidate
                for candidate in candidates
                if candidate.head_repo_slug.lower() == repo_slug
            ]
            lookup_detail: dict[str, object] = {
                "status": "found" if same_repo else "not_found",
                "candidate_count": len(candidates),
                "same_repo_count": len(same_repo),
                "fork_collision_count": len(candidates) - len(same_repo),
            }
            if not same_repo:
                reconcile_lookups.append(lookup_detail)
                return None
            match = same_repo[0]
            reconciled_match = match
            lookup_detail["matched_pr"] = _branch_open_pr_metadata(match)
            reconcile_lookups.append(lookup_detail)
            return match.url

        try:
            result = await self._run_gh_command(
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
                operation="gh pr create",
                retry_policy=RetryPolicy.RECONCILABLE_MUTATION,
                reconciler=_reconcile_create_pr,
                on_failure=lambda detail: failures.append(detail),
                allow_duplicate_retry=lambda: duplicate_lookup_failed,
                max_attempts=transient_max_attempts,
            )
        except GitHubClientError:
            self._last_pr_create_outcome = PullRequestCreateTransportOutcome(
                strategy="failed",
                attempts=len(failures) or 1,
                failures=failures,
                reconcile_lookups=reconcile_lookups,
            )
            raise

        match = _PR_URL_PATTERN.search(result.stdout)
        if match is None:
            self._last_pr_create_outcome = PullRequestCreateTransportOutcome(
                strategy="failed",
                attempts=len(failures) + 1 if failures else 1,
                failures=failures,
                reconcile_lookups=reconcile_lookups,
            )
            raise GitHubClientError(
                operation="gh pr create (no URL in stdout)",
                returncode=0,
                stderr=f"unexpected gh output: {result.stdout.strip()[:500]}",
            )

        duplicate_seen = any(bool(item.get("duplicate")) for item in failures)
        if failures and reconciled_match is not None:
            strategy = (
                "reconciled_after_duplicate" if duplicate_seen else "reconciled_after_transient"
            )
            matched_pr = _branch_open_pr_metadata(reconciled_match)
            attempts = len(failures)
        elif failures:
            strategy = "created_after_retry"
            matched_pr = None
            attempts = len(failures) + 1
        else:
            strategy = "direct"
            matched_pr = None
            attempts = 1

        if failures or reconciled_match is not None:
            self._last_pr_create_outcome = PullRequestCreateTransportOutcome(
                strategy=strategy,
                attempts=attempts,
                failures=failures,
                reconcile_lookups=reconcile_lookups,
                matched_pr=matched_pr,
            )
        else:
            self._last_pr_create_outcome = None

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
        # No pre-merge state check here: the monitor's pre-merge recheck
        # (``_fetch_status_for_decision``) already refreshes mergeability + head-SHA
        # immediately before calling this, so a duplicate ``gh pr view`` would only add
        # a redundant round-trip (and, being a read, a retry point that misclassifies a
        # merge rejection as a transient). ``merge_pr`` just executes the merge; an
        # already-merged/out-of-band PR surfaces as a ``gh pr merge`` error the merge
        # loop classifies.
        args = ["gh", "pr", "merge", str(pr_number), "--repo", repo.slug(), f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        # NEVER-retry: a merge failure is usually semantic (branch protection,
        # required approvals, behind-base, head changed — Codex #7), not a network
        # transient, and the unknown->TRANSIENT default would otherwise retry a
        # rejection. The monitor re-runs its full merge gate (fresh mergeability +
        # head-SHA) on the next cycle, so a genuinely transient merge blip is retried
        # at the cycle level with fresh state rather than blindly in-cycle.
        await self._run_gh_command(
            args,
            operation="gh pr merge",
            retry_policy=RetryPolicy.NEVER,
        )
        try:
            sha_result = await self._run_gh_command(
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
                operation="gh pr view merge commit",
                # NEVER-retry: ``merge_pr`` runs inside the monitor's serialized
                # merge critical section, and this post-merge SHA read is best-effort
                # (a failure returns "" and the caller falls back to the head SHA via
                # ``_merge_completion_marker``). A ``READ`` policy would sleep+retry a
                # transient blip *while holding the merge lock*, contradicting the
                # fail-fast ``retry=False`` contract other forge reads honor under the
                # same lock (merge_loop.py pre-merge recheck). One attempt, then fall
                # back — the merge already succeeded, so the SHA is metadata only.
                retry_policy=RetryPolicy.NEVER,
            )
        except GitHubClientError:
            return ""
        return sha_result.stdout.strip()

    async def _run_gh_command(
        self,
        args: list[str],
        *,
        operation: str,
        retry_policy: RetryPolicy,
        reconciler: Reconciler | None = None,
        on_failure: Callable[[dict[str, object]], None] | None = None,
        allow_duplicate_retry: Callable[[], bool] | None = None,
        max_attempts: int | None = None,
        response_validator: Callable[[CommandResult], str | None] | None = None,
    ) -> CommandResult:
        try:
            return await _execute_gh_with_retry(
                self._runner,
                args,
                operation=operation,
                retry_policy=retry_policy,
                reconciler=reconciler,
                on_failure=on_failure,
                sleep=self._sleep,
                allow_duplicate_retry=allow_duplicate_retry,
                max_attempts=max_attempts,
                response_validator=response_validator,
            )
        except GitHubClientError:
            raise
        except (OSError, TimeoutError) as exc:
            # Convert only genuine transport/subprocess faults — e.g. the ``gh``
            # binary missing (``OSError``/``FileNotFoundError``) or a runner-level
            # timeout — into the structured ``GitHubClientError`` callers expect. A
            # bare ``except Exception`` here re-wrapped programming errors
            # (``TypeError``/``AttributeError``/``ValueError``, or a
            # ``response_validator`` bug) as generic ``returncode=1`` gh failures,
            # masking real bugs as transport faults; those now surface unmasked
            # (AGENTS.md: catch specific exceptions, not bare ``Exception``).
            raise GitHubClientError(
                operation=operation,
                returncode=1,
                stderr=str(exc),
            ) from exc

    # ── Internals ──────────────────────────────────────────────────────────

    async def _graphql(
        self,
        *,
        query: str,
        variables: dict[str, Any],
        retry_policy: RetryPolicy = RetryPolicy.READ,
    ) -> dict[str, Any]:
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
        result = await self._run_gh_command(
            args,
            operation="gh api graphql",
            retry_policy=retry_policy,
            response_validator=_transient_graphql_payload_error,
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
        return payload  # type: ignore[no-any-return]

    async def _gh_json(
        self,
        args: list[str],
        *,
        operation: str,
        retry_policy: RetryPolicy = RetryPolicy.NEVER,
    ) -> Any:
        """Run a GH CLI JSON command and decode the stdout body.

        Reads default to ``NEVER``: the transport's allow-by-default retry is for
        one-shot mutations (create/reconcile). Monitor reads run in a poll loop that
        re-polls on failure, and one-shot reads either classify their own error or
        surface it to the user, so an in-cycle retry here would be redundant and would
        retry into a stale result. Callers pass an explicit policy to opt in.
        """
        result = await self._run_gh_command(
            args,
            operation=operation,
            retry_policy=retry_policy,
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

    async def _run_gh(
        self,
        args: list[str],
        *,
        operation: str,
        strict: bool,
        retry_policy: RetryPolicy = RetryPolicy.NEVER,
    ) -> Any:
        """Execute a GH CLI command, optionally enforcing success.

        Reads default to ``NEVER`` (see ``_gh_json``); the transport's retry is for
        one-shot mutations. Callers pass an explicit policy to opt into retry.
        """
        try:
            result = await self._run_gh_command(
                args,
                operation=operation,
                retry_policy=retry_policy,
            )
        except GitHubClientError as exc:
            if strict:
                raise
            # ``strict=False`` callers (best-effort fetches such as purged CI logs)
            # tolerate failure and read an empty result. The centralized transport
            # raises on a permanent / retry-exhausted fault, so translate it back into
            # a non-ok CommandResult here instead of aborting the caller.
            return CommandResult(
                returncode=exc.returncode or 1,
                stdout="",
                stderr=getattr(exc, "stderr", ""),
            )
        # ``result`` is only bound when ``_run_gh_command`` did not raise (i.e. it is
        # ``ok``); the centralized transport raises on any non-zero exit, so a
        # ``strict`` failure has already propagated above.
        return result


# ── Tiny helpers kept private to avoid accidental imports ──────────────────


from awf.common.github_client_parsing import (  # noqa: E402
    _FAILED_CHECK_CONCLUSIONS,
    _actions_run_id_from_details_url,
    _clean_optional_str,
    _connection_nodes,
    _dig,
    _effective_blocking_reviews,
    _extract_pr_file_paths,
    _flatten_branch_rules_pages,
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
    _rollup_check_failure_conclusion,
    _rollup_check_has_external_details_url,
    _rollup_check_is_explicit_github_actions,
    _rollup_check_is_github_actions,
    _tail,
)
