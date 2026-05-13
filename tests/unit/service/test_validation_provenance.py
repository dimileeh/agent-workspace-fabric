"""Validation provenance service tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import awf.service.validation_provenance as validation_service


@pytest.mark.unit
def test_validation_provenance_command_lookup_resolves_database_hooks() -> None:
    workspace = SimpleNamespace(
        resolved_profile={
            "name": "service-provenance-db-hooks",
            "database": {
                "generated_setup": ["python scripts/db_generated_setup.py"],
                "pre_validation_refresh": ["python scripts/db_refresh.py"],
            },
        },
        test_commands=["pytest -q"],
    )

    lookup = validation_service._command_lookup(workspace)

    assert lookup[("db_generated_setup", 1)] == "python scripts/db_generated_setup.py"
    assert lookup[("db_refresh", 1)] == "python scripts/db_refresh.py"
    assert lookup[("validate", 1)] == "pytest -q"
