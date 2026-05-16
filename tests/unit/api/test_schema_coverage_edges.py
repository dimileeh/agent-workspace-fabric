"""Focused API schema edge tests for compatibility validators."""

from __future__ import annotations

import pytest

from awf.api import schemas as api_schemas


@pytest.mark.unit
def test_workspace_reason_compatibility_request_keeps_normal_reason_body() -> None:
    request = api_schemas._WorkspaceReasonCompatibilityRequest.model_validate(  # noqa: SLF001
        {"reason": "operator requested"}
    )

    assert request.reason == "operator requested"
