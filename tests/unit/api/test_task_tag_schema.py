"""Schema-level validation tests for the workspace task tag (Jira issue key)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.api.schemas import (
    PullRequestMonitorAdoptionRequest,
    WorkspaceCreateRequest,
    WorkspaceTask,
)


def _create_request(task_tag: str | None | object = "__unset__") -> WorkspaceCreateRequest:
    task: dict[str, object] = {
        "title": "Add a thing",
        "prompt": "Do the work.",
        "agent": "codex",
        "kind": "feature_branch_pr",
    }
    if task_tag != "__unset__":
        task["task_tag"] = task_tag
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/app.git", "base_branch": "development"},
        task=task,
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": [], "requested_tier": 1},
        resources={},
    )


@pytest.mark.unit
def test_workspace_task_accepts_valid_tag() -> None:
    task = WorkspaceTask(title="t", prompt="p", task_tag="PROJ-123")
    assert task.task_tag == "PROJ-123"


@pytest.mark.unit
def test_workspace_task_absent_tag_is_none() -> None:
    task = WorkspaceTask(title="t", prompt="p")
    assert task.task_tag is None


@pytest.mark.unit
def test_workspace_task_rejects_malformed_tag() -> None:
    with pytest.raises(ValidationError, match="task tag"):
        WorkspaceTask(title="t", prompt="p", task_tag="proj-123")


@pytest.mark.unit
def test_create_request_exposes_task_tag_property() -> None:
    assert _create_request("PROJ-123").task_tag == "PROJ-123"
    assert _create_request().task_tag is None


@pytest.mark.unit
def test_create_request_rejects_malformed_tag() -> None:
    with pytest.raises(ValidationError, match="task tag"):
        _create_request("PROJ123")


@pytest.mark.unit
def test_adoption_request_accepts_and_validates_tag() -> None:
    request = PullRequestMonitorAdoptionRequest(pr_url="https://x/pr/1", task_tag="AB-1")
    assert request.task_tag == "AB-1"

    absent = PullRequestMonitorAdoptionRequest(pr_url="https://x/pr/1")
    assert absent.task_tag is None

    with pytest.raises(ValidationError, match="task tag"):
        PullRequestMonitorAdoptionRequest(pr_url="https://x/pr/1", task_tag="bad")
