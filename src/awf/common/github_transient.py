"""Shared GitHub transient-failure classification helpers."""

from __future__ import annotations

NON_TRANSIENT_GITHUB_ERROR_MARKERS = (
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
    # #515 symmetry with Bitbucket: 401/bad-credentials can be a transient auth
    # blip and is bounded-retried; deterministic not-logged-in/not-configured
    # markers above still win first and remain terminal.
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
)


def is_transient_github_error_text(*, operation: str, stderr: str) -> bool:
    """Return whether a GitHub CLI/API failure looks transient."""

    operation_text = operation.lower()
    stderr_text = stderr.lower()
    text = f"{operation_text}\n{stderr_text}"
    if any(marker in text for marker in NON_TRANSIENT_GITHUB_ERROR_MARKERS):
        return False
    has_bad_credentials = "bad credentials" in stderr_text
    has_auth_transient_evidence = any(
        marker in stderr_text for marker in GITHUB_AUTH_TRANSIENT_EVIDENCE_MARKERS
    )
    has_ambiguous_auth_transient_context = any(
        marker in operation_text for marker in GITHUB_AMBIGUOUS_AUTH_TRANSIENT_CONTEXT_MARKERS
    )
    if has_bad_credentials and not has_ambiguous_auth_transient_context:
        return False
    has_resubmit_guidance = any(marker in text for marker in GITHUB_RESUBMIT_TRANSIENT_MARKERS)
    has_api_context = any(marker in text for marker in GITHUB_API_CONTEXT_MARKERS)
    if has_resubmit_guidance and has_api_context:
        if has_bad_credentials and not has_auth_transient_evidence:
            return False
        if has_auth_transient_evidence and operation_text.startswith("gh pr create"):
            return False
        return not (has_auth_transient_evidence and not has_ambiguous_auth_transient_context)
    if has_auth_transient_evidence and operation_text.startswith("gh pr create"):
        return False
    if any(marker in text for marker in TRANSIENT_GITHUB_ERROR_MARKERS):
        return True
    if not any(marker in stderr_text for marker in AMBIGUOUS_GITHUB_AUTH_TRANSIENT_MARKERS):
        return False
    if operation_text.startswith("gh pr create"):
        return False
    return has_ambiguous_auth_transient_context
