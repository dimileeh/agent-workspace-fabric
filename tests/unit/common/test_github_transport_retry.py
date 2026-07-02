"""Transport-level GitHub retry, timeout, and reconcile coverage."""

from __future__ import annotations

import json
import time

import pytest

from awf.common.commands import COMMAND_TIMEOUT_REASON, FakeCommandRunner
from awf.common.github_client import (
    GITHUB_API_ERROR,
    GitHubClient,
    GitHubClientError,
    RepoRef,
)
from awf.common.github_retry import (
    GITHUB_TRANSPORT_MAX_ATTEMPTS,
    GitHubRetryContext,
    RetryPolicy,
    github_retry_context,
    past_transport_deadline,
)
from awf.common.github_transport import execute_gh_with_retry as _execute_gh_with_retry


class _RecordedSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _open_pr_list_payload(*, number: int = 99, repo_slug: str = "o/r") -> str:
    owner, repo = repo_slug.split("/", 1)
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/{repo_slug}/pull/{number}",
                "headRefName": "feature",
                "headRefOid": "a" * 40,
                "headRepository": {"name": repo, "nameWithOwner": repo_slug},
                "headRepositoryOwner": {"login": owner},
            }
        ]
    )


@pytest.mark.unit
async def test_read_transient_retries_then_succeeds() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner.queue_result(returncode=0, stdout='{"ok": true}')
    sleep = _RecordedSleep()

    result = await _execute_gh_with_retry(
        runner,
        ["gh", "api", "repos/o/r"],
        operation="gh api repo",
        retry_policy=RetryPolicy.READ,
        sleep=sleep,
    )

    assert result.ok
    assert len(runner.calls) == 2
    assert len(sleep.calls) == 1


@pytest.mark.unit
async def test_permanent_error_does_not_retry() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="not logged in to any GitHub hosts")

    with pytest.raises(GitHubClientError) as exc_info:
        await _execute_gh_with_retry(
            runner,
            ["gh", "api", "graphql"],
            operation="gh api graphql",
            retry_policy=RetryPolicy.READ,
            sleep=_RecordedSleep(),
        )

    assert len(runner.calls) == 1
    assert exc_info.value.reason_code == GITHUB_API_ERROR


@pytest.mark.unit
async def test_response_validator_retries_transient_payload_then_succeeds() -> None:
    # gh api graphql exits 0 on an HTTP-200 body; the first body carries a
    # transient GraphQL errors array, the second is clean. The validator routes
    # the exit-0 payload error through the same retry machinery as a stderr fault.
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=0,
        stdout='{"errors": [{"message": "something went wrong"}]}',
    )
    runner.queue_result(returncode=0, stdout='{"data": {"ok": true}}')
    sleep = _RecordedSleep()

    def _validator(result: object) -> str | None:
        payload = json.loads(result.stdout)  # type: ignore[attr-defined]
        errors = payload.get("errors")
        return json.dumps(errors) if errors else None

    result = await _execute_gh_with_retry(
        runner,
        ["gh", "api", "graphql"],
        operation="gh api graphql",
        retry_policy=RetryPolicy.READ,
        sleep=sleep,
        response_validator=_validator,
    )

    assert result.ok
    assert json.loads(result.stdout)["data"]["ok"] is True
    assert len(runner.calls) == 2
    assert len(sleep.calls) == 1


@pytest.mark.unit
async def test_response_validator_permanent_payload_raises_without_retry() -> None:
    # A permanent payload error surfaced by the validator classifies PERMANENT and
    # fails fast — the transport must not retry it.
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=0,
        stdout='{"errors": [{"message": "could not resolve to a node"}]}',
    )
    sleep = _RecordedSleep()

    with pytest.raises(GitHubClientError, match="could not resolve"):
        await _execute_gh_with_retry(
            runner,
            ["gh", "api", "graphql"],
            operation="gh api graphql",
            retry_policy=RetryPolicy.READ,
            sleep=sleep,
            response_validator=lambda r: json.dumps(json.loads(r.stdout)["errors"]),
        )

    assert len(runner.calls) == 1
    assert sleep.calls == []


@pytest.mark.unit
async def test_response_validator_none_accepts_result() -> None:
    # A validator that returns None leaves an exit-0 result untouched (no retry).
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout='{"data": {"ok": true}}')

    result = await _execute_gh_with_retry(
        runner,
        ["gh", "api", "graphql"],
        operation="gh api graphql",
        retry_policy=RetryPolicy.READ,
        sleep=_RecordedSleep(),
        response_validator=lambda _r: None,
    )

    assert result.ok
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_never_policy_raises_without_retry() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 503 Service Unavailable")

    with pytest.raises(GitHubClientError):
        await _execute_gh_with_retry(
            runner,
            ["gh", "pr", "comment", "1"],
            operation="gh pr comment",
            retry_policy=RetryPolicy.NEVER,
            sleep=_RecordedSleep(),
        )

    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_transport_respects_top_level_deadline() -> None:
    runner = FakeCommandRunner()
    for _ in range(GITHUB_TRANSPORT_MAX_ATTEMPTS + 2):
        runner.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    token = github_retry_context.set(
        GitHubRetryContext(deadline=time.monotonic() - 1.0),
    )
    try:
        with pytest.raises(GitHubClientError):
            await _execute_gh_with_retry(
                runner,
                ["gh", "api", "graphql"],
                operation="gh api graphql",
                retry_policy=RetryPolicy.READ,
                sleep=_RecordedSleep(),
            )
    finally:
        github_retry_context.reset(token)
    assert len(runner.calls) == 0


@pytest.mark.unit
async def test_hung_gh_counts_as_transient_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "awf.common.github_transport.GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS",
        0.05,
    )
    runner = FakeCommandRunner()
    runner.queue_hang()
    runner.queue_result(returncode=0, stdout='{"ok": true}')

    result = await _execute_gh_with_retry(
        runner,
        ["gh", "api", "graphql"],
        operation="gh api graphql",
        retry_policy=RetryPolicy.READ,
        sleep=_RecordedSleep(),
    )

    assert result.ok
    assert len(runner.calls) == 2
    assert runner.calls[0].timeout_seconds == 0.05


@pytest.mark.unit
async def test_create_pr_transient_retry_opens_exactly_one_pr() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=1,
        stderr="gh api graphql failed (exit=1): error connecting to api.github.com",
    )
    runner.queue_result(returncode=0, stdout=_open_pr_list_payload())
    gh = GitHubClient(runner, sleep=_RecordedSleep())

    url = await gh.create_pull_request(
        repo=RepoRef(owner="o", name="r"),
        base="main",
        head="feature",
        title="t",
        body="b",
    )

    assert url == "https://github.com/o/r/pull/99"
    create_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]
    assert len(create_calls) == 1
    outcome = gh.last_pr_create_outcome
    assert outcome is not None
    assert outcome.strategy == "reconciled_after_transient"


@pytest.mark.unit
async def test_post_comment_transient_does_not_retry() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 503 Service Unavailable")
    gh = GitHubClient(runner, sleep=_RecordedSleep())

    with pytest.raises(GitHubClientError):
        await gh.post_comment(repo=RepoRef(owner="o", name="r"), pr_number=1, body="hi")

    comment_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1


@pytest.mark.unit
async def test_no_hang_wall_clock_bounded_with_zero_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    for _ in range(GITHUB_TRANSPORT_MAX_ATTEMPTS):
        runner.queue_hang()
    monkeypatch.setattr(
        "awf.common.github_transport.jittered_backoff_seconds",
        lambda **_: 0.0,
    )
    monkeypatch.setattr(
        "awf.common.github_transport.GITHUB_TRANSPORT_PER_ATTEMPT_TIMEOUT_SECONDS",
        0.05,
    )

    started = time.monotonic()
    with pytest.raises(GitHubClientError):
        await _execute_gh_with_retry(
            runner,
            ["gh", "api", "graphql"],
            operation="gh api graphql",
            retry_policy=RetryPolicy.READ,
            sleep=_RecordedSleep(),
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0


@pytest.mark.unit
def test_past_transport_deadline_helper() -> None:
    assert past_transport_deadline(time.monotonic() - 1.0)
    assert not past_transport_deadline(time.monotonic() + 60.0)
    assert not past_transport_deadline(None)


@pytest.mark.unit
async def test_is_duplicate_pull_request_error_classification() -> None:
    from awf.common.github_transport import _is_duplicate_pull_request_error

    dup = GitHubClientError(
        operation="gh pr create", returncode=1, stderr="a pull request already exists for o:feature"
    )
    assert _is_duplicate_pull_request_error(dup) is True
    other = GitHubClientError(operation="gh pr create", returncode=1, stderr="no commits between")
    assert _is_duplicate_pull_request_error(other) is False
    # An "already exists" failure from an unrelated operation (label, ref, ...) must
    # not be misclassified as a duplicate PR now that the check is scoped to pr-create.
    unrelated = GitHubClientError(
        operation="gh label create", returncode=1, stderr="label already exists"
    )
    assert _is_duplicate_pull_request_error(unrelated) is False
    assert _is_duplicate_pull_request_error("not a github error") is False


@pytest.mark.unit
async def test_max_attempts_override_caps_in_transport_retry() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    with pytest.raises(GitHubClientError):
        await _execute_gh_with_retry(
            runner,
            ["gh", "api", "x"],
            operation="gh api",
            retry_policy=RetryPolicy.READ,
            sleep=_RecordedSleep(),
            max_attempts=1,
        )
    assert len(runner.calls) == 1  # a transient is NOT retried when max_attempts=1


@pytest.mark.unit
async def test_timeout_reason_code_preserved_on_exhaustion() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=124,
        stderr="gh api timed out after 30s",
        reason_code=COMMAND_TIMEOUT_REASON,
    )
    with pytest.raises(GitHubClientError) as exc:
        await _execute_gh_with_retry(
            runner,
            ["gh", "api", "x"],
            operation="gh api",
            retry_policy=RetryPolicy.READ,
            sleep=_RecordedSleep(),
            max_attempts=1,
        )
    # The COMMAND_TIMEOUT provenance must survive the wrapper, not collapse to
    # a generic GITHUB_API_ERROR (AGENTS.md: retries preserve reason codes).
    assert exc.value.reason_code == COMMAND_TIMEOUT_REASON


@pytest.mark.unit
async def test_cycle_deadline_before_first_attempt_raises_deadline_error() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="{}")
    token = github_retry_context.set(GitHubRetryContext(deadline=time.monotonic() - 100.0))
    try:
        with pytest.raises(GitHubClientError) as exc:
            await _execute_gh_with_retry(
                runner,
                ["gh", "api", "x"],
                operation="gh api",
                retry_policy=RetryPolicy.READ,
                sleep=_RecordedSleep(),
            )
        assert "deadline" in exc.value.stderr.lower()
    finally:
        github_retry_context.reset(token)
    assert len(runner.calls) == 0  # deadline tripped before any command ran


@pytest.mark.unit
async def test_cycle_deadline_after_first_failure_raises_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = {"n": 0}

    def fake_past(deadline: float | None) -> bool:
        checks["n"] += 1
        return checks["n"] >= 2  # not past on the first check, past on the second

    monkeypatch.setattr("awf.common.github_transport.past_transport_deadline", fake_past)
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    token = github_retry_context.set(GitHubRetryContext(deadline=time.monotonic() + 100.0))
    try:
        with pytest.raises(GitHubClientError) as exc:
            await _execute_gh_with_retry(
                runner,
                ["gh", "api", "x"],
                operation="gh api",
                retry_policy=RetryPolicy.READ,
                sleep=_RecordedSleep(),
            )
        assert "502" in exc.value.stderr  # the preserved last error, not the deadline sentinel
    finally:
        github_retry_context.reset(token)
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_never_policy_ignores_stale_cycle_deadline() -> None:
    # A NEVER-policy call issues exactly one attempt and cannot contribute to a
    # retry storm, so a cycle deadline already consumed by a long preceding
    # AddressComments fix/validation pass or settle wait must NOT short-circuit
    # it. Otherwise the first post-fix settle/recheck/resolve/comment/merge call
    # raises a fabricated "cycle deadline exceeded" without ever contacting the
    # forge and can terminate a healthy workspace (PRRT_kwDOSJAM6s6N-X5D).
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout='{"ok": true}')
    token = github_retry_context.set(GitHubRetryContext(deadline=time.monotonic() - 100.0))
    try:
        result = await _execute_gh_with_retry(
            runner,
            ["gh", "pr", "merge", "1"],
            operation="gh pr merge",
            retry_policy=RetryPolicy.NEVER,
            sleep=_RecordedSleep(),
        )
    finally:
        github_retry_context.reset(token)
    assert result.ok
    assert len(runner.calls) == 1  # the stale deadline did not short-circuit the call


@pytest.mark.unit
async def test_never_policy_surfaces_real_error_despite_stale_deadline() -> None:
    # Same guard, failure path: a NEVER call reached with a stale deadline must
    # still contact the forge and surface the *real* error, not the fabricated
    # deadline sentinel — preserving the true reason code for the caller.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr="HTTP 422 Unprocessable Entity")
    token = github_retry_context.set(GitHubRetryContext(deadline=time.monotonic() - 100.0))
    try:
        with pytest.raises(GitHubClientError) as exc:
            await _execute_gh_with_retry(
                runner,
                ["gh", "pr", "comment", "1"],
                operation="gh pr comment",
                retry_policy=RetryPolicy.NEVER,
                sleep=_RecordedSleep(),
            )
    finally:
        github_retry_context.reset(token)
    assert "422" in exc.value.stderr  # the real forge error, not the deadline sentinel
    assert "deadline" not in exc.value.stderr.lower()
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_secret_stderr_redacted_before_logging_and_persisting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # gh/GraphQL stderr can embed raw tokens; the live transport_retry warning
    # and the persisted error_message payload must both be redacted, not just
    # truncated (AGENTS.md: redact secrets for live logs and persisted payloads).
    secret = "ghp_should_not_persist"
    stderr = (
        f"remote: https://x-access-token:{secret}@github.com/org/repo "
        f"Authorization: Bearer {secret}"
    )
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stderr=stderr)
    runner.queue_result(returncode=0, stdout='{"ok": true}')
    persisted: list[dict[str, object]] = []

    with caplog.at_level("WARNING"):
        result = await _execute_gh_with_retry(
            runner,
            ["gh", "api", "repos/o/r"],
            operation="gh api repo",
            retry_policy=RetryPolicy.READ,
            sleep=_RecordedSleep(),
            on_failure=persisted.append,
        )

    assert result.ok
    assert secret not in caplog.text  # live warning must not leak the token
    assert secret not in str(persisted[0]["error_message"])  # nor the persisted payload
    assert "<redacted>" in str(persisted[0]["error_message"])
