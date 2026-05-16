"""Regression tests for database model metadata."""

from __future__ import annotations

import pytest

from awf.db.models import Operation, WorkspaceSecretLease


@pytest.mark.unit
def test_operations_idempotency_lookup_has_covering_index() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in Operation.__table__.indexes
    }

    assert indexes["ix_operations_idempotency_key_created_at_id"] == (
        "idempotency_key",
        "created_at",
        "id",
    )


@pytest.mark.unit
def test_workspace_secret_leases_have_workspace_status_and_expiry_indexes() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in WorkspaceSecretLease.__table__.indexes
    }

    assert indexes["ix_workspace_secret_leases_workspace_status"] == (
        "workspace_id",
        "status",
    )
    assert indexes["ix_workspace_secret_leases_status_expires"] == (
        "status",
        "expires_at",
    )
