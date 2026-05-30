from __future__ import annotations

from awf.db.models import Operation
from awf.service.controls_helpers import _operation_result_warnings


def test_operation_result_warnings_replays_json_list_dict_entries() -> None:
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
        {"warning_code": "REMONITOR_PAST_SETTLE", "message": "review grace elapsed"},
        {"warning_code": "STALE_HEAD"},
    ]


def test_operation_result_warnings_ignores_non_json_sequence_shapes() -> None:
    operation = Operation(result={"warnings": ({"warning_code": "REMONITOR_PAST_SETTLE"},)})

    assert _operation_result_warnings(operation) == []
