"""Shared GitHub transient-failure classification helpers."""

from __future__ import annotations

from enum import StrEnum

NON_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "not logged in",
    "please run gh auth login",
    "not found",
    "could not resolve to a repository",
    "could not resolve to a node",
)

PERMANENT_GITHUB_ERROR_MARKERS = (
    *NON_TRANSIENT_GITHUB_ERROR_MARKERS,
    "permission denied",
    "http 403",
    "access denied",
    "sso authorization required",
    "sso required",
    "resource not accessible",
    "branch protection",
    "repository is archived",
    "repo is archived",
    "was archived",
    "auth token expired",
    "review is required",
    "review required before merging",
    "cannot find a field",
    "doesn't exist on type",
    "unknown field",
    "graphql schema",
    "could not be merged with this method",
    "no commits between",
    "squash merges are not allowed",
    "merge method squash merging is not allowed",
    "merge commits are not allowed",
    "merge method merge commit is not allowed",
    "rebase merges are not allowed",
    "merge method rebase is not allowed",
)

TRANSIENT_GITHUB_ERROR_MARKERS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "returned error: 500",
    "returned error: 502",
    "returned error: 503",
    "returned error: 504",
    "service unavailable",
    "temporarily unavailable",
    "try again",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "error connecting to",
    "tls handshake timeout",
    "network",
    "eof",
    "rate limit",
    "secondary rate limit",
    "abuse detection",
    "something went wrong",
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve proxy",
)

RATE_LIMIT_GITHUB_ERROR_MARKERS = (
    # GitHub reports primary/secondary rate-limit throttling as HTTP 403 while the
    # body carries one of these recoverable markers. They must win over the generic
    # "http 403"/"access denied" PERMANENT markers so the monitor retries the
    # throttle instead of terminating; genuinely permanent 403s (permission denied,
    # resource not accessible, sso required, ...) never mention a rate limit.
    "rate limit",
    "secondary rate limit",
    "abuse detection",
)

AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS = (
    # #515 symmetry with Bitbucket: 401 auth failures can be transient auth
    # blips and are bounded-retried; deterministic not-logged-in/not-configured
    # markers above still win first and remain terminal. Bare bad-credentials
    # errors still need 401/requires-authentication evidence before retrying.
    "bad credentials",
    "http 401",
    "requires authentication",
)

GITHUB_AUTH_TRANSIENT_EVIDENCE_MARKERS = (
    "http 401",
    "requires authentication",
)

GITHUB_RESUBMIT_TRANSIENT_MARKERS = (
    "please try resubmitting",
    "try resubmitting",
)

GITHUB_API_CONTEXT_MARKERS = (
    "api.github.com",
    "github api",
    "graphql",
    "gh api",
    "gh pr create",
)

GITHUB_AUTH_TRANSIENT_CONTEXT_MARKERS = (
    "api.github.com",
    "github api",
    "graphql",
    "gh api",
)

GITHUB_AMBIGUOUS_AUTH_TRANSIENT_CONTEXT_MARKERS = (
    *GITHUB_AUTH_TRANSIENT_CONTEXT_MARKERS,
    "gh pr ",
    "gh issue create",
    "list_runs_for_sha",
    "view_run_log",
)


class GitHubErrorDisposition(StrEnum):
    """Disposition for a gh failure.

    Two layers read this with deliberately different defaults for ``UNKNOWN``:

    * The **transport** (``execute_gh_with_retry``) retries ``{TRANSIENT, UNKNOWN}``
      in-cycle — allow-by-default, so a blip with an unrecognized phrasing (e.g. the
      ws_88b71225 connection error) recovers without a marker being added.
    * The **monitor** terminate-vs-keep-polling decision
      (``_is_transient_github_client_error``) keeps a workspace alive only on a
      ``{TRANSIENT, AMBIGUOUS_AUTH}`` fault it recognizes, and FAILS on ``UNKNOWN``
      rather than polling forever on a fault it cannot classify.
    """

    PERMANENT = "permanent"
    AMBIGUOUS_AUTH = "ambiguous_auth"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


def github_error_disposition(*, operation: str, stderr: str) -> GitHubErrorDisposition:
    """Classify a GitHub CLI/API failure for transport retry policy."""

    operation_text = operation.lower()
    stderr_text = stderr.lower()
    text = f"{operation_text}\n{stderr_text}"

    has_rate_limit_marker = any(marker in text for marker in RATE_LIMIT_GITHUB_ERROR_MARKERS)

    # Scope the "already exists" exclusion to the duplicate-PR-create case only. A
    # bare "already exists" must not blanket-suppress the whole PERMANENT marker set,
    # or a stderr combining it with e.g. "permission denied" / "branch protection"
    # would be retried in-cycle instead of failing fast. ``gh pr create`` duplicate
    # errors always name the "pull request", so requiring both markers is safe.
    duplicate_pr_marker = "already exists" in stderr_text and "pull request" in stderr_text

    if (
        not has_rate_limit_marker
        and not duplicate_pr_marker
        and any(marker in text for marker in PERMANENT_GITHUB_ERROR_MARKERS)
    ):
        return GitHubErrorDisposition.PERMANENT

    has_bad_credentials = "bad credentials" in stderr_text
    has_auth_transient_evidence = any(
        marker in stderr_text for marker in GITHUB_AUTH_TRANSIENT_EVIDENCE_MARKERS
    )
    has_ambiguous_auth_transient_context = any(
        marker in operation_text for marker in GITHUB_AMBIGUOUS_AUTH_TRANSIENT_CONTEXT_MARKERS
    )
    if has_bad_credentials and not has_auth_transient_evidence:
        return GitHubErrorDisposition.PERMANENT
    if has_bad_credentials and not has_ambiguous_auth_transient_context:
        return GitHubErrorDisposition.PERMANENT

    has_resubmit_guidance = any(marker in text for marker in GITHUB_RESUBMIT_TRANSIENT_MARKERS)
    has_api_context = any(marker in text for marker in GITHUB_API_CONTEXT_MARKERS)
    if has_resubmit_guidance and has_api_context:
        if has_auth_transient_evidence and operation_text.startswith("gh pr create"):
            return GitHubErrorDisposition.PERMANENT
        if has_auth_transient_evidence and not has_ambiguous_auth_transient_context:
            return GitHubErrorDisposition.PERMANENT
        return GitHubErrorDisposition.TRANSIENT

    if has_auth_transient_evidence and operation_text.startswith("gh pr create"):
        return GitHubErrorDisposition.PERMANENT

    if any(marker in stderr_text for marker in AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS):
        if has_ambiguous_auth_transient_context:
            return GitHubErrorDisposition.AMBIGUOUS_AUTH
        return GitHubErrorDisposition.PERMANENT

    # A KNOWN network / 5xx / timeout / rate-limit marker → TRANSIENT (the monitor
    # keeps the workspace alive and re-polls). Anything else non-permanent is
    # UNKNOWN: the transport still retries it in-cycle, but the monitor FAILS on it.
    if any(marker in text for marker in TRANSIENT_GITHUB_ERROR_MARKERS):
        return GitHubErrorDisposition.TRANSIENT
    return GitHubErrorDisposition.UNKNOWN


def is_transient_github_error_text(*, operation: str, stderr: str) -> bool:
    """Return whether a GitHub CLI/API failure looks transient."""

    disposition = github_error_disposition(operation=operation, stderr=stderr)
    return disposition in {
        GitHubErrorDisposition.TRANSIENT,
        GitHubErrorDisposition.AMBIGUOUS_AUTH,
    }
