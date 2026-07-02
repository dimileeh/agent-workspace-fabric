"""GitHub CLI transport retry, timeout, and error disposition handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from awf.common.commands import COMMAND_TIMEOUT_REASON, AsyncCommandRunner, CommandResult
from awf.common.github_retry import (
    GITHUB_TRANSPORT_MAX_ATTEMPTS,
    GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS,
    Reconciler,
    RetryPolicy,
    github_retry_context,
    jittered_backoff_seconds,
    past_transport_deadline,
)
from awf.common.github_transient import (
    GitHubErrorDisposition,
    github_error_disposition,
)
from awf.common.logging import get_logger

_log = get_logger(__name__)

_TIMEOUT_RETURN_CODE = 124


def _short_github_error(text: str) -> str:
    cleaned = text.strip() or "<no output>"
    return cleaned[:1000]


def _is_duplicate_pull_request_error(error: object) -> bool:
    from awf.common.github_client import GitHubClientError

    if not isinstance(error, GitHubClientError):
        return False
    return error.returncode == 1 and "already exists" in error.stderr.lower()


def _github_failure_detail(
    *,
    attempt: int,
    operation: str,
    returncode: int,
    stderr: str,
    disposition: GitHubErrorDisposition | None = None,
    duplicate: bool = False,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "attempt": attempt,
        "operation": operation,
        "returncode": returncode,
        "error_message": _short_github_error(stderr),
        "duplicate": duplicate,
        "will_retry": False,
    }
    if disposition is not None:
        detail["disposition"] = disposition.value
        detail["transient"] = disposition in {
            GitHubErrorDisposition.TRANSIENT,
            GitHubErrorDisposition.AMBIGUOUS_AUTH,
        }
    return detail


async def execute_gh_with_retry(
    runner: AsyncCommandRunner,
    args: list[str],
    *,
    operation: str,
    retry_policy: RetryPolicy,
    reconciler: Reconciler | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_failure: Callable[[dict[str, object]], None] | None = None,
    allow_duplicate_retry: Callable[[], bool] | None = None,
    max_attempts: int | None = None,
) -> CommandResult:
    """Run gh with bounded retry, per-attempt timeout, and explicit policy.

    ``max_attempts`` overrides the default attempt cap for callers that own their
    own retry budget (e.g. ``create_pull_request`` drives it from
    ``pr_create_transient_max_retries``); ``None`` uses the transport default.
    """

    from awf.common.github_client import GITHUB_API_ERROR, GitHubClientError

    sleep_fn = sleep or asyncio.sleep
    ctx = github_retry_context.get()
    deadline = ctx.deadline if ctx is not None else None
    last_error: GitHubClientError | None = None
    attempt_cap = max(
        1, max_attempts if max_attempts is not None else GITHUB_TRANSPORT_MAX_ATTEMPTS
    )

    for attempt in range(1, attempt_cap + 1):
        if past_transport_deadline(deadline):
            if last_error is not None:
                _log.warning(
                    "github.transport_retry_exhausted",
                    workspace_id=ctx.workspace_id if ctx is not None else None,
                    operation=operation,
                    attempt_count=attempt - 1,
                    max_attempts=attempt_cap,
                    retry_policy=retry_policy.value,
                    pr_number=ctx.pr_number if ctx is not None else None,
                    reason="cycle_deadline",
                )
                raise last_error
            raise GitHubClientError(
                operation=operation,
                returncode=_TIMEOUT_RETURN_CODE,
                stderr="github transport cycle deadline exceeded",
            )

        result = await runner.run(
            args,
            timeout_seconds=GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS,
        )
        if result.ok:
            return result

        stderr = result.stderr
        if result.reason_code == COMMAND_TIMEOUT_REASON:
            stderr = f"{operation} timed out: {result.stderr}"

        error = GitHubClientError(
            operation=operation,
            returncode=result.returncode,
            stderr=stderr,
            # Preserve the runner's reason code (e.g. COMMAND_TIMEOUT) so the
            # monitor records the timeout provenance instead of a generic
            # GITHUB_API_ERROR once retries exhaust (AGENTS.md retry rule).
            reason_code=result.reason_code or GITHUB_API_ERROR,
        )
        last_error = error
        duplicate = _is_duplicate_pull_request_error(error)
        disposition = github_error_disposition(operation=operation, stderr=stderr)
        failure_detail = _github_failure_detail(
            attempt=attempt,
            operation=operation,
            returncode=result.returncode,
            stderr=stderr,
            disposition=disposition,
            duplicate=duplicate,
        )
        if on_failure is not None:
            on_failure(failure_detail)

        if retry_policy == RetryPolicy.NEVER:
            raise error

        if disposition == GitHubErrorDisposition.PERMANENT:
            raise error

        if retry_policy == RetryPolicy.RECONCILABLE_MUTATION and reconciler is not None:
            reconciled = await reconciler()
            if reconciled is not None:
                return CommandResult(returncode=0, stdout=str(reconciled), stderr="")
            if duplicate and not (allow_duplicate_retry and allow_duplicate_retry()):
                raise error

        # Allow-by-default: retry both known-network TRANSIENT and UNKNOWN faults
        # in-cycle, so a blip with an unrecognized phrasing (ws_88b71225's
        # "error connecting to") recovers without a marker being added. PERMANENT
        # fails fast (handled above). AMBIGUOUS_AUTH (HTTP 401) is deliberately NOT
        # retried here: retrying with the same token in-cycle is futile and would
        # bypass the #515 path where the monitor re-polls and the token is re-resolved
        # at the poll boundary — it propagates to the caller's auth handling.
        if disposition not in {
            GitHubErrorDisposition.TRANSIENT,
            GitHubErrorDisposition.UNKNOWN,
        }:
            raise error

        if attempt >= attempt_cap:
            break

        wait_seconds = jittered_backoff_seconds(attempt=attempt)
        failure_detail["will_retry"] = True
        failure_detail["wait_seconds"] = wait_seconds
        _log.warning(
            "github.transport_retry",
            workspace_id=ctx.workspace_id if ctx is not None else None,
            operation=operation,
            attempt=attempt,
            max_attempts=attempt_cap,
            disposition=disposition.value,
            retry_policy=retry_policy.value,
            pr_number=ctx.pr_number if ctx is not None else None,
            wait_seconds=wait_seconds,
            stderr=_short_github_error(stderr)[:200],
        )
        if wait_seconds > 0:
            await sleep_fn(wait_seconds)

    _log.warning(
        "github.transport_retry_exhausted",
        workspace_id=ctx.workspace_id if ctx is not None else None,
        operation=operation,
        attempt_count=attempt_cap,
        max_attempts=attempt_cap,
        retry_policy=retry_policy.value,
        pr_number=ctx.pr_number if ctx is not None else None,
    )
    assert last_error is not None
    raise last_error
