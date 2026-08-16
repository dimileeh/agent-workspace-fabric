"""Schema-level validation for external task ids (no ASCII control chars)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.api.schemas import PullRequestMonitorAdoptionRequest, WorkspaceTask
from awf.common.external_id import validate_external_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "good-id",
        "CLOUD-TASK-42",
        "a" * 128,
        "id with spaces",
        "unicode-é",
    ],
)
def test_validate_external_id_accepts_printable_values(value: str) -> None:
    assert validate_external_id(value) == value
    assert validate_external_id(None) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "has\x00nul",
        "\x00",
        "lead\ttab",
        "line\nbreak",
        "bell\x07",
        "del\x7f",
    ],
)
def test_validate_external_id_rejects_ascii_controls(value: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        validate_external_id(value)


@pytest.mark.unit
def test_adoption_request_rejects_nul_in_external_id() -> None:
    with pytest.raises(ValidationError, match="control characters"):
        PullRequestMonitorAdoptionRequest(
            repo_slug="dimileeh/aira-web",
            pr_number=277,
            external_id="CLOUD\x00TASK",
        )


@pytest.mark.unit
def test_adoption_request_accepts_plain_external_id() -> None:
    request = PullRequestMonitorAdoptionRequest(
        repo_slug="dimileeh/aira-web",
        pr_number=277,
        external_id="CLOUD-TASK-42",
    )
    assert request.external_id == "CLOUD-TASK-42"


@pytest.mark.unit
def test_workspace_task_rejects_nul_in_external_id() -> None:
    with pytest.raises(ValidationError, match="control characters"):
        WorkspaceTask(title="t", prompt="p", external_id="x\x00y")


@pytest.mark.unit
def test_workspace_task_accepts_plain_external_id() -> None:
    task = WorkspaceTask(title="t", prompt="p", external_id="CLOUD-TASK-42")
    assert task.external_id == "CLOUD-TASK-42"
