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
from awf.common.redaction import redact_secrets

_log = get_logger(__name__)


def _short_github_error(text: str) -> str:
    # Redact before truncating so gh/GraphQL stderr embedding raw tokens never
    # reaches the live github.transport_retry warning or the persisted
    # error_message payload verbatim (AGENTS.md: redact secrets for live logs).
    cleaned = redact_secrets(text.strip()) or "<no output>"
    return cleaned[:1000]


def _is_duplicate_pull_request_error(error: object) -> bool:
    from awf.common.github_client import GitHubClientError

    if not isinstance(error, GitHubClientError):
        return False
    # Scope to the create-PR path. ``execute_gh_with_retry`` is a generic transport
    # for many gh/GraphQL operations, so an unrelated "already exists" failure (a
    # label, ref, etc.) must not be misclassified as a duplicate PR and routed into
    # PR-create reconciliation or flagged ``duplicate`` in the failure audit detail.
    return (
        error.returncode == 1
        and "pr create" in error.operation.lower()
        and "already exists" in error.stderr.lower()
    )


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
    response_validator: Callable[[CommandResult], str | None] | None = None,
) -> CommandResult:
    """Run gh with bounded retry, per-attempt timeout, and explicit policy.

    ``max_attempts`` overrides the default attempt cap for callers that own their
    own retry budget (e.g. ``create_pull_request`` drives it from
    ``pr_create_transient_max_retries``); ``None`` uses the transport default.

    ``response_validator`` inspects an otherwise-successful (exit 0) result and
    returns error text when the *body* signals a logical failure the process exit
    did not — e.g. ``gh api graphql`` exits 0 on an HTTP-200 GraphQL ``errors``
    payload (gh only surfaces errors for HTTP >= 400). Returning a string routes
    that body through the same disposition/retry machinery as a non-zero exit, so
    a transient server-side GraphQL blip is retried under the caller's policy
    instead of bypassing the transport; ``None`` accepts the result as-is.
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
        # The cycle deadline bounds *retry* storms — many calls each retrying for
        # tens of minutes within one monitor poll — so it gates only attempts that
        # follow a failure (``last_error is not None``), never a call's *first*
        # attempt. A first attempt is a single request bounded by the per-attempt
        # timeout and cannot contribute to a storm; the same reasoning already
        # exempts NEVER-policy calls (one attempt only). Gating first attempts is
        # what fabricated a "cycle deadline exceeded" error without ever contacting
        # the forge — terminating a healthy workspace as an UNKNOWN GITHUB_API_ERROR
        # when a long preceding fix/validation pass consumed the budget
        # (PRRT_kwDOSJAM6s6N-X5D) or when earlier *successful* paginated reads in the
        # same status snapshot spent it, so a large/slow PR's later pages died even
        # though GitHub never failed (PRRT_kwDOSJAM6s6OB6L_). Retries still stay
        # bounded, and the caller sees the real last error, not a synthetic one.
        if (
            retry_policy != RetryPolicy.NEVER
            and last_error is not None
            and past_transport_deadline(deadline)
        ):
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

        result = await runner.run(
            args,
            timeout_seconds=GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS,
        )
        payload_error: str | None = None
        if result.ok:
            if response_validator is not None:
                try:
                    payload_error = response_validator(result)
                except Exception as validator_exc:
                    # A caller-supplied ``response_validator`` raising is a contract
                    # violation, not a gh transport fault, but it must still reach
                    # callers as the structured ``GitHubClientError`` the transport
                    # guarantees. Callers such as
                    # ``fetch_pull_request_adoption_metadata`` and
                    # ``list_open_pull_requests_for_branch`` invoke this transport
                    # directly and only catch ``GitHubClientError``, and
                    # ``_run_gh_command`` no longer blanket-wraps unexpected
                    # exceptions — so without this the crash would escape as an
                    # unclassified type, breaking the "reason codes flow end-to-end"
                    # contract for this one surface. Fail fast (a validator bug is
                    # deterministic; retrying is futile) with the default
                    # ``GITHUB_API_ERROR`` reason code. The broad catch is a
                    # deliberate boundary around foreign, caller-supplied code.
                    raise GitHubClientError(
                        operation=operation,
                        returncode=1,
                        stderr=f"response_validator raised: {validator_exc}",
                    ) from validator_exc
            if payload_error is None:
                return result

        if payload_error is not None:
            # An exit-0 result whose body carries a logical failure (e.g. a GraphQL
            # HTTP-200 ``errors`` payload). Synthesize a returncode-0 failure so the
            # disposition/retry logic below classifies and retries it under the same
            # policy as a stderr-surfaced fault instead of the caller having to raise
            # past the transport.
            stderr = payload_error
            returncode = 0
            reason_code = GITHUB_API_ERROR
        else:
            stderr = result.stderr
            if result.reason_code == COMMAND_TIMEOUT_REASON:
                stderr = f"{operation} timed out: {result.stderr}"
            returncode = result.returncode
            # Preserve the runner's reason code (e.g. COMMAND_TIMEOUT) so the
            # monitor records the timeout provenance instead of a generic
            # GITHUB_API_ERROR once retries exhaust (AGENTS.md retry rule).
            reason_code = result.reason_code or GITHUB_API_ERROR

        error = GitHubClientError(
            operation=operation,
            returncode=returncode,
            stderr=stderr,
            reason_code=reason_code,
        )
        last_error = error
        duplicate = _is_duplicate_pull_request_error(error)
        disposition = github_error_disposition(operation=operation, stderr=stderr)
        failure_detail = _github_failure_detail(
            attempt=attempt,
            operation=operation,
            returncode=returncode,
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
