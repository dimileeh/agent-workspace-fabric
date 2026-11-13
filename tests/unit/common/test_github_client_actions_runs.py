"""``GitHubClient.fetch_actions_runs_state`` reads the head's Actions runs (#1230)."""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.runtime.pr_monitor_models import ActionsRunsState

_REPO = RepoRef(owner="o", name="r")


class _RecordedSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _runs(*statuses: str) -> str:
    return json.dumps(
        [{"databaseId": index, "status": status} for index, status in enumerate(statuses)]
    )


async def _state(stdout: str, *, returncode: int = 0, stderr: str = "") -> ActionsRunsState:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
    return await GitHubClient(fake).fetch_actions_runs_state(
        repo=_REPO, head_sha="deadbeef", retry=False
    )


@pytest.mark.unit
@pytest.mark.parametrize("pending", ["queued", "in_progress", "waiting", "requested", "pending"])
async def test_any_non_completed_run_is_pending(pending: str) -> None:
    assert await _state(_runs("completed", pending)) is ActionsRunsState.PENDING


@pytest.mark.unit
async def test_status_comparison_is_case_insensitive_and_missing_status_is_pending() -> None:
    assert await _state(_runs("COMPLETED")) is ActionsRunsState.SETTLED
    assert await _state(json.dumps([{"databaseId": 1}])) is ActionsRunsState.PENDING


@pytest.mark.unit
async def test_all_completed_runs_are_settled() -> None:
    assert await _state(_runs("completed", "completed")) is ActionsRunsState.SETTLED


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ["[]", ""])
async def test_head_without_actions_runs_does_not_block(stdout: str) -> None:
    assert await _state(stdout) is ActionsRunsState.SETTLED


@pytest.mark.unit
async def test_lists_only_actions_runs_for_the_head_sha() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=0, stdout="[]")

    await GitHubClient(fake).fetch_actions_runs_state(repo=_REPO, head_sha="deadbeef")

    args = fake.calls[0].args
    assert args[:3] == ["gh", "run", "list"]
    assert args[args.index("--repo") + 1] == "o/r"
    assert args[args.index("--commit") + 1] == "deadbeef"
    assert args[args.index("--json") + 1] == "databaseId,status"


@pytest.mark.unit
async def test_non_zero_exit_raises() -> None:
    with pytest.raises(GitHubClientError, match="not found"):
        await _state("", returncode=1, stderr="not found")


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ['{"runs": []}', "[1]"])
async def test_malformed_payload_raises(stdout: str) -> None:
    with pytest.raises(GitHubClientError, match="list_actions_runs_for_sha"):
        await _state(stdout)


@pytest.mark.unit
async def test_retry_false_fails_fast_without_backoff() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    sleep = _RecordedSleep()

    with pytest.raises(GitHubClientError, match="502"):
        await GitHubClient(fake, sleep=sleep).fetch_actions_runs_state(
            repo=_REPO, head_sha="deadbeef", retry=False
        )

    assert len(fake.calls) == 1
    assert sleep.calls == []


@pytest.mark.unit
async def test_polling_default_retries_a_transient_blip() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    fake.queue_result(returncode=0, stdout=_runs("queued"))

    state = await GitHubClient(fake, sleep=_RecordedSleep()).fetch_actions_runs_state(
        repo=_REPO, head_sha="deadbeef"
    )

    assert state is ActionsRunsState.PENDING
    assert len(fake.calls) == 2
