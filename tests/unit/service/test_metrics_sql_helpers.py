"""Coverage for PostgreSQL SQL helper compilers used by metrics modules."""

from __future__ import annotations

import pytest
from sqlalchemy import literal_column, select
from sqlalchemy.dialects import postgresql

from awf.service import metrics, metrics_capacity, metrics_slo


def _compiled_sql(function_element: object) -> str:
    statement = select(function_element)
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.unit
@pytest.mark.parametrize(
    "function_element",
    [
        metrics._IsoToTimestamp(literal_column("workspace_events.payload ->> 'ts'")),  # noqa: SLF001
        metrics_capacity._IsoToTimestamp(literal_column("payload ->> 'queued_at'")),  # noqa: SLF001
        metrics_slo._IsoToTimestamp(literal_column("payload ->> 'completed_at'")),  # noqa: SLF001
    ],
)
def test_iso_to_timestamp_compiles_postgres_guarded_cast(function_element: object) -> None:
    sql = _compiled_sql(function_element)

    assert "CASE WHEN" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql


@pytest.mark.unit
async def test_capacity_scan_returns_empty_when_page_limits_are_zero() -> None:
    candidates = await metrics_capacity._provider_recovery_eligible_capacity_queue_scan_candidates(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        node_id="node-1",
        resource_defaults=object(),  # type: ignore[arg-type]
        limit=0,
        max_refill_pages=10,
        scoring_at=object(),  # type: ignore[arg-type]
    )

    assert candidates == []
