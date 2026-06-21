"""Shared GitHub transient-failure classification helpers."""

from __future__ import annotations

NON_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "bad credentials",
    "not logged in",
    "please run gh auth login",
    "not found",
    "could not resolve to a repository",
    "could not resolve to a node",
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

AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS = (
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


def is_transient_github_error_text(*, operation: str, stderr: str) -> bool:
    """Return whether a GitHub CLI/API failure looks transient."""

    text = f"{operation}\n{stderr}".lower()
    if any(marker in text for marker in NON_TRANSIENT_GITHUB_ERROR_MARKERS):
        return False
    if any(marker in text for marker in GITHUB_RESUBMIT_TRANSIENT_MARKERS) and any(
        marker in text for marker in GITHUB_API_CONTEXT_MARKERS
    ):
        return True
    if any(marker in text for marker in TRANSIENT_GITHUB_ERROR_MARKERS):
        return True
    return any(marker in text for marker in AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS)
