"""Workspace control response schema tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.api.schemas import WorkspaceControlResponse
from awf.db.enums import OperationStatus, WorkspaceStatus


@pytest.mark.unit
def test_workspace_control_response_rejects_unknown_operation_status() -> None:
    with pytest.raises(ValidationError):
        WorkspaceControlResponse(
            workspace_id="ws_control",
            operation_id="op_control",
            operation_status="not-a-real-status",
            status=WorkspaceStatus.running,
            message="workspace operation started",
        )


@pytest.mark.unit
def test_workspace_control_response_accepts_operation_status_enum_values() -> None:
    response = WorkspaceControlResponse(
        workspace_id="ws_control",
        operation_id="op_control",
        operation_status=OperationStatus.running,
        status=WorkspaceStatus.running,
        message="workspace operation started",
    )

    assert response.operation_status == OperationStatus.running


@pytest.mark.unit
def test_workspace_control_response_schema_exposes_operation_status_enum() -> None:
    schema = WorkspaceControlResponse.model_json_schema()

    assert schema["properties"]["operation_status"] == {"$ref": "#/$defs/OperationStatus"}
    assert schema["$defs"]["OperationStatus"]["enum"] == [
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
