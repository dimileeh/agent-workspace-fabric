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


class WorkspaceRetryPrStateUnavailableError(WorkspaceRetryError):
    """Raised when retry cannot safely establish an existing PR's live identity."""

    error_code = "WORKSPACE_RETRY_PR_STATE_UNAVAILABLE"


class WorkspaceRetryPrAlreadyMergedError(WorkspaceRetryError):
    """Raised when retry discovers that the source PR is already merged."""

    error_code = "PR_ALREADY_MERGED"
