"""Focused workspace retry pull-request lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import awf.service.workspaces_retry as workspaces_retry_service
import awf.service.workspaces_retry_runtime as workspaces_retry_runtime
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.service.workspaces import WorkspaceRetryNotFoundError
from awf.service.workspaces_retry import _live_pr_lifecycle, _pr_number_from_url

pytestmark = pytest.mark.unit


def test_retry_not_found_error_has_instance_detail() -> None:
    error = WorkspaceRetryNotFoundError("ws_missing")
    assert error.detail is None
    assert error.__dict__["detail"] is None


@pytest.mark.parametrize(
    "pr_url",
    [
        "https://github.com/example/retryable/pull/0",
        "https://github.com/example/retryable/issues/10",
        "not-a-pr-url",
    ],
)
def test_pr_number_from_url_rejects_invalid_or_non_positive_number(pr_url: str) -> None:
    assert _pr_number_from_url(pr_url) is None


async def test_live_pr_lifecycle_uses_current_forge_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeForgeClient:
        async def __aenter__(self) -> FakeForgeClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def fetch_pull_request_lifecycle(self, **kwargs: object) -> PullRequestLifecycle:
            calls.append(kwargs)
            return PullRequestLifecycle.merged

    monkeypatch.setattr(
        workspaces_retry_runtime,
        "make_forge_client",
        lambda _forge, _runner: FakeForgeClient(),
    )
    source = SimpleNamespace(
        repo_url="git@github.com:example/retryable.git",
        resolved_profile={"forge": "github"},
    )
    assert await _live_pr_lifecycle(source, 10) is PullRequestLifecycle.merged
    assert calls == [
        {
            "repo": workspaces_retry_service.RepoRef(
                owner="example", name="retryable", forge="github"
            ),
            "pr_number": 10,
        }
    ]
