"""Validation provenance service helper tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import awf.service.validation_provenance as validation_service


@pytest.mark.unit
def test_validation_provenance_command_lookup_uses_request_validation_commands() -> None:
    workspace = SimpleNamespace(
        resolved_profile=None,
        test_commands=["pytest -q", "ruff check"],
    )

    lookup = validation_service._command_lookup(workspace)

    assert lookup == {
        ("validate", 1): "pytest -q",
        ("validate", 2): "ruff check",
    }
