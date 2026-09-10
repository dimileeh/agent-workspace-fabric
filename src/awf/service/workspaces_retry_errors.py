"""Errors raised while retrying a workspace."""

from typing import Any


class WorkspaceRetryError(Exception):
    """Base error for failures encountered while retrying a workspace."""

    error_code = "WORKSPACE_RETRY_ERROR"
    message = "Workspace retry failed."
    detail: dict[str, Any] | None

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Initialise with an optional override message and structured detail."""
        if message is not None:
            self.message = message
        self.detail = detail
        super().__init__(self.message)


class WorkspaceRetryIdempotencyConflictError(WorkspaceRetryError):
    """Raised when a retry idempotency key is reused for another request."""

    error_code = "IDEMPOTENCY_CONFLICT"
    message = (
        "Idempotency-Key previously used with a different retry request; "
        "supply a fresh key or replay the original request."
    )


class WorkspaceRetryIdempotencyReplayUnavailableError(WorkspaceRetryError):
    """Raised when a persisted retry operation cannot reconstruct its response."""

    error_code = "IDEMPOTENCY_REPLAY_UNAVAILABLE"
    message = "The original workspace retry result is no longer available for replay."


class WorkspaceRetryPrStateUnavailableError(WorkspaceRetryError):
    """Raised when retry cannot safely establish an existing PR's live identity."""

    error_code = "WORKSPACE_RETRY_PR_STATE_UNAVAILABLE"


class WorkspaceRetryPrAlreadyMergedError(WorkspaceRetryError):
    """Raised when retry discovers that the source PR is already merged."""

    error_code = "PR_ALREADY_MERGED"


class WorkspaceHostedDelegationNotConfiguredError(WorkspaceRetryError):
    """Raised when hosted PR-adoption retry lacks hosted delegation settings."""

    error_code = "HOSTED_DELEGATION_NOT_CONFIGURED"
    message = "Hosted PR adoption retry requires configured hosted delegation settings."
