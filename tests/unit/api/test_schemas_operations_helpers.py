"""Small coverage tests for operation schema helper facades."""

from __future__ import annotations

import pytest

from awf.api import schemas_operations


@pytest.mark.unit
def test_log_stream_helper_facades_delegate_to_canonical_helpers() -> None:
    refs = {"agent": ["stdout", {"nested": "stderr"}]}

    assert schemas_operations._log_stream_ids(refs) == ["stderr", "stdout"]  # noqa: SLF001
    assert schemas_operations.merge_log_stream_ref_value(["stdout"], ["stderr"]) == [
        "stdout",
        "stderr",
    ]
