"""Regression tests for database model metadata."""

from __future__ import annotations

import pytest

from awf.db.models import Operation


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
