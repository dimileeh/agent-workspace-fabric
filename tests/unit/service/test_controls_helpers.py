from __future__ import annotations

from awf.api.schemas import WorkspaceControlWarningResponse
from awf.db.models import Operation
from awf.service.controls_helpers import _operation_result_warnings


def test_operation_result_warnings_replays_valid_json_warning_entries() -> None:
    operation = Operation(
        result={
            "warnings": [
                {"warning_code": "REMONITOR_PAST_SETTLE", "message": "review grace elapsed"},
                "legacy",
                {"warning_code": "STALE_HEAD"},
            ]
        }
    )

    assert _operation_result_warnings(operation) == [
        WorkspaceControlWarningResponse(
            warning_code="REMONITOR_PAST_SETTLE",
            message="review grace elapsed",
        )
    ]


def test_operation_result_warnings_ignores_non_json_sequence_shapes() -> None:
    operation = Operation(result={"warnings": ({"warning_code": "REMONITOR_PAST_SETTLE"},)})

    assert _operation_result_warnings(operation) == []
