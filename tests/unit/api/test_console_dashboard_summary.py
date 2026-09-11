"""Console dashboard-summary API tests (schema_version=1)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.routes.console import ConsoleDashboardSummaryResponse
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.unit.helpers import create_workspace

_FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "console" / "fixtures" / "v1"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPENAPI_JSON = _REPO_ROOT / "openapi.json"
_COUNT_KEYS = (
    "active",
    "executing",
    "monitoring_pr",
    "awaiting_operator",
    "awaiting_human",
    "retrying",
    "queued",
    "completed_last_window",
    "cancelled_last_window",
    "failed_last_window",
)


def _dashboard_summary_payload() -> dict[str, Any]:
    return json.loads((_FIXTURES / "dashboard-summary.local.json").read_text(encoding="utf-8"))


def _zero_confirmed_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNT_KEYS, 0)


def _summary_with_count_evidence(
    *,
    total: int = 29,
    known: int = 24,
    unknown: int = 5,
    confirmed_patch: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload = _dashboard_summary_payload()
    payload["coverage"] = {"status": "partial", "notes": ["workflow_status_incomplete"]}
    payload["counts"] = dict.fromkeys(_COUNT_KEYS, None)
    confirmed = _zero_confirmed_counts()
    confirmed.update(confirmed_patch or {})
    payload["count_evidence"] = {
        "total_workspaces": total,
        "status_known_workspaces": known,
        "status_unknown_workspaces": unknown,
        "confirmed_counts": confirmed,
    }
    return payload


def _rewrite_component_refs(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            assert isinstance(ref, str)
            assert ref.startswith("#/components/schemas/")
            return {"$ref": f"urn:awf:schemas/{ref.rsplit('/', 1)[-1]}"}
        return {key: _rewrite_component_refs(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_component_refs(item) for item in obj]
    return obj


def _dashboard_summary_openapi_validator() -> Draft202012Validator:
    spec = json.loads(_OPENAPI_JSON.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    rewritten = {
        f"urn:awf:schemas/{name}": Resource(
            contents=_rewrite_component_refs(schema),
            specification=DRAFT202012,
        )
        for name, schema in schemas.items()
    }
    registry = Registry().with_resources(rewritten.items())
    root = _rewrite_component_refs(copy.deepcopy(schemas["ConsoleDashboardSummaryResponse"]))
    return Draft202012Validator(root, registry=registry)


@pytest.mark.unit
async def test_dashboard_summary_requires_auth(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/console/dashboard-summary",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_dashboard_summary_independent_of_capacity(
    client: AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary must not call Docker/capacity scanners."""

    now = datetime.now(UTC)
    for status in (
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.blocked,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.requested,
        WorkspaceStatus.requested,
    ):
        await create_workspace(engine, status=status, updated_at=now)

    flagged = await create_workspace(engine, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    factory = make_session_factory(engine)
    async with factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            flagged, reason="blocking review requires a human", now=now
        )
        await session.commit()

    await create_workspace(
        engine,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=1),
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=2),
    )
    await create_workspace(
        engine,
        status=WorkspaceStatus.cancelled,
        updated_at=now - timedelta(hours=3),
    )

    docker_probe = AsyncMock(side_effect=AssertionError("Docker must not be probed"))
    monkeypatch.setattr(
        "awf.service.orphan_resources.scan_docker_resources",
        docker_probe,
    )
    monkeypatch.setattr(
        "awf.service.local_capacity.detect_local_capacity",
        docker_probe,
    )

    response = await client.get("/v1/console/dashboard-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["scope"] == "local"
    assert body["coverage"]["status"] == "complete"
    counts = body["counts"]
    # running+validating+pushing = 3 executing; blocked+2 monitoring + 2 requested + 3 exec = 8 active
    assert counts["executing"] == 3
    assert counts["awaiting_operator"] == 1
    assert counts["awaiting_human"] == 1
    assert counts["monitoring_pr"] == 2
    assert counts["queued"] == 2
    assert counts["retrying"] == 0
    assert counts["active"] == 8
    assert counts["completed_last_window"] == 1
    assert counts["failed_last_window"] == 1
    assert counts["cancelled_last_window"] == 1
    assert body["overlap"]["awaiting_human_subset_of_monitoring_pr"] is True
    assert body["overlap"]["awaiting_operator_in_active_not_executing"] is True
    assert body["window"]["anchor"] == "generated_at"
    assert body["window"]["since_hours"] == 24
    assert "count_evidence" not in body
    docker_probe.assert_not_called()


@pytest.mark.unit
async def test_dashboard_summary_null_not_zero_on_partial(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.console_dashboard_summary import (
        ConsoleDashboardCounts,
        ConsoleDashboardCoverage,
        ConsoleDashboardOverlap,
        ConsoleDashboardSummary,
        ConsoleDashboardWindow,
    )

    generated = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)

    async def _partial_summary(*_args, **_kwargs):
        return ConsoleDashboardSummary(
            schema_version=1,
            scope="local",
            generated_at=generated,
            as_of=generated,
            last_success_at=generated,
            window=ConsoleDashboardWindow(
                anchor="generated_at",
                since_hours=24,
                start=generated - timedelta(hours=24),
            ),
            coverage=ConsoleDashboardCoverage(
                status="partial",
                notes=("queued_count_unavailable",),
            ),
            counts=ConsoleDashboardCounts(
                active=3,
                executing=2,
                monitoring_pr=1,
                awaiting_operator=0,
                awaiting_human=0,
                retrying=0,
                queued=None,
                completed_last_window=1,
                cancelled_last_window=None,
                failed_last_window=0,
            ),
            overlap=ConsoleDashboardOverlap(
                awaiting_human_subset_of_monitoring_pr=True,
                awaiting_operator_in_active_not_executing=True,
                retrying_in_active_not_executing=True,
            ),
        )

    monkeypatch.setattr(
        "awf.api.routes.console.summarize_console_dashboard_for_session",
        _partial_summary,
    )
    response = await client.get("/v1/console/dashboard-summary")
    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["status"] == "partial"
    assert body["counts"]["queued"] is None
    assert body["counts"]["cancelled_last_window"] is None
    assert "queued" in body["counts"]
    assert body["counts"]["failed_last_window"] == 0


@pytest.mark.unit
def test_dashboard_summary_count_evidence_is_optional_nullable_and_omitted_when_unused() -> None:
    payload = _dashboard_summary_payload()
    absent = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert absent.count_evidence is None
    assert "count_evidence" not in absent.model_dump(mode="json")

    payload["count_evidence"] = None
    explicit_null = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert explicit_null.count_evidence is None
    assert "count_evidence" not in explicit_null.model_dump(mode="json")


@pytest.mark.unit
def test_dashboard_summary_accepts_confirmed_lower_bound_examples() -> None:
    all_zero = ConsoleDashboardSummaryResponse.model_validate(_summary_with_count_evidence())
    assert all_zero.count_evidence is not None
    assert all_zero.count_evidence.total_workspaces == 29
    assert all_zero.count_evidence.status_known_workspaces == 24
    assert all_zero.count_evidence.status_unknown_workspaces == 5
    assert all_zero.count_evidence.confirmed_counts.active == 0

    running_payload = _summary_with_count_evidence(
        total=30,
        known=25,
        unknown=5,
        confirmed_patch={"active": 1, "executing": 1},
    )
    running = ConsoleDashboardSummaryResponse.model_validate(running_payload)
    assert running.count_evidence is not None
    assert running.count_evidence.confirmed_counts.active == 1
    assert running.count_evidence.confirmed_counts.executing == 1
    assert running.counts.active is None


@pytest.mark.unit
@pytest.mark.parametrize("object_name", ["count_evidence", "confirmed_counts"])
def test_dashboard_summary_count_evidence_rejects_unknown_object_keys(object_name: str) -> None:
    payload = _summary_with_count_evidence()
    target = payload["count_evidence"]
    if object_name == "confirmed_counts":
        target = target["confirmed_counts"]
    target["unpublished_field"] = 0
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("object_name", "required_key"),
    [
        ("count_evidence", "total_workspaces"),
        ("count_evidence", "status_known_workspaces"),
        ("count_evidence", "status_unknown_workspaces"),
        ("count_evidence", "confirmed_counts"),
        *(("confirmed_counts", key) for key in _COUNT_KEYS),
    ],
)
def test_dashboard_summary_count_evidence_rejects_missing_required_fields(
    object_name: str,
    required_key: str,
) -> None:
    payload = _summary_with_count_evidence()
    target = payload["count_evidence"]
    if object_name == "confirmed_counts":
        target = target["confirmed_counts"]
    del target[required_key]
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize("invalid", ["1", True, -1, 1.5])
@pytest.mark.parametrize(
    "field_path",
    [
        ("total_workspaces",),
        ("status_known_workspaces",),
        ("status_unknown_workspaces",),
        ("confirmed_counts", "active"),
    ],
)
def test_dashboard_summary_count_evidence_rejects_non_strict_nonnegative_integers(
    field_path: tuple[str, ...],
    invalid: object,
) -> None:
    payload = _summary_with_count_evidence()
    target = payload["count_evidence"]
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = invalid
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_count_evidence_rejects_population_contradictions() -> None:
    bad_sum = _summary_with_count_evidence(total=30, known=24, unknown=5)
    with pytest.raises(ValidationError, match="known.*unknown.*total"):
        ConsoleDashboardSummaryResponse.model_validate(bad_sum)

    above_known = _summary_with_count_evidence(confirmed_patch={"active": 25})
    with pytest.raises(ValidationError, match="confirmed.*known"):
        ConsoleDashboardSummaryResponse.model_validate(above_known)

    disjoint_statuses_above_known = _summary_with_count_evidence(
        confirmed_patch={
            "active": 20,
            "completed_last_window": 10,
            "cancelled_last_window": 10,
            "failed_last_window": 10,
        }
    )
    with pytest.raises(ValidationError, match="status categories.*known"):
        ConsoleDashboardSummaryResponse.model_validate(disjoint_statuses_above_known)

    complete_with_unknown_status = _summary_with_count_evidence(total=1, known=0, unknown=1)
    complete_with_unknown_status["coverage"] = {"status": "complete", "notes": []}
    complete_with_unknown_status["counts"] = _zero_confirmed_counts()
    with pytest.raises(ValidationError, match="complete.*unknown"):
        ConsoleDashboardSummaryResponse.model_validate(complete_with_unknown_status)


@pytest.mark.unit
@pytest.mark.parametrize(
    "confirmed_patch",
    [
        {"active": 1, "executing": 2},
        {"active": 1, "monitoring_pr": 2, "awaiting_human": 0},
        {"active": 1, "queued": 2},
        {"active": 2, "monitoring_pr": 1, "awaiting_human": 2},
        {"active": 2, "executing": 2, "awaiting_operator": 1},
        {"active": 2, "executing": 2, "retrying": 1},
        {
            "active": 3,
            "executing": 1,
            "monitoring_pr": 1,
            "awaiting_operator": 1,
            "retrying": 1,
        },
        {"active": 1, "executing": 1, "queued": 1},
    ],
)
def test_dashboard_summary_count_evidence_rejects_relationship_contradictions(
    confirmed_patch: dict[str, int],
) -> None:
    payload = _summary_with_count_evidence(confirmed_patch=confirmed_patch)
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize("count_key", _COUNT_KEYS)
def test_dashboard_summary_count_evidence_rejects_exact_count_mismatch(count_key: str) -> None:
    payload = _summary_with_count_evidence()
    payload["counts"][count_key] = 1
    with pytest.raises(ValidationError, match="exact.*confirmed"):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_count_evidence_keeps_coverage_reasons_independent() -> None:
    payload = _summary_with_count_evidence(total=1, known=1, unknown=0)
    payload["coverage"]["notes"] = [
        "terminal_timestamp_unavailable",
        "attention_evidence_unavailable",
    ]
    payload["counts"].update(_zero_confirmed_counts())
    payload["counts"]["completed_last_window"] = None
    payload["counts"]["awaiting_human"] = None
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.coverage.status == "partial"
    assert model.count_evidence is not None
    assert model.count_evidence.status_unknown_workspaces == 0
    assert model.counts.completed_last_window is None
    assert model.counts.awaiting_human is None
    assert model.coverage.notes == [
        "terminal_timestamp_unavailable",
        "attention_evidence_unavailable",
    ]


@pytest.mark.unit
def test_dashboard_summary_openapi_exposes_strict_optional_count_evidence() -> None:
    validator = _dashboard_summary_openapi_validator()
    legacy = _dashboard_summary_payload()
    assert not list(validator.iter_errors(legacy))

    explicit_null = copy.deepcopy(legacy)
    explicit_null["count_evidence"] = None
    assert not list(validator.iter_errors(explicit_null))

    valid = _summary_with_count_evidence()
    assert not list(validator.iter_errors(valid))

    malformed_payloads: list[dict[str, Any]] = []
    for field_path, invalid in (
        (("total_workspaces",), "29"),
        (("status_known_workspaces",), True),
        (("status_unknown_workspaces",), -1),
        (("confirmed_counts", "active"), 0.5),
    ):
        payload = _summary_with_count_evidence()
        target = payload["count_evidence"]
        for key in field_path[:-1]:
            target = target[key]
        target[field_path[-1]] = invalid
        malformed_payloads.append(payload)

    missing = _summary_with_count_evidence()
    del missing["count_evidence"]["confirmed_counts"]["queued"]
    malformed_payloads.append(missing)
    extra = _summary_with_count_evidence()
    extra["count_evidence"]["extra"] = 0
    malformed_payloads.append(extra)

    for malformed in malformed_payloads:
        assert list(validator.iter_errors(malformed))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_path", "numeric_ts"),
    [
        (("generated_at",), 0),
        (("as_of",), 1),
        (("last_success_at",), 1_694_000_000),
        (("window", "start"), 1_694_000_000.5),
    ],
)
def test_dashboard_summary_rejects_numeric_timestamps(
    field_path: tuple[str, ...],
    numeric_ts: float,
) -> None:
    """Match the shipped TS parser: dashboard timestamps must be ISO strings."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    target: Any = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = numeric_ts
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_accepts_iso_timestamp_strings() -> None:
    payload = _dashboard_summary_payload()
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.generated_at.year == 2026
    assert model.window.start.year == 2026
    assert model.generated_at.tzinfo is not None
    assert model.generated_at.utcoffset() is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_path", "naive_value"),
    [
        (("generated_at",), "2026-09-07T12:00:00"),
        (("as_of",), "2026-09-07T12:00:00.123"),
        (("last_success_at",), datetime(2026, 9, 7, 12, 0, 0)),
        (("window", "start"), datetime(2026, 9, 6, 12, 0, 0)),
    ],
)
def test_dashboard_summary_rejects_timezone_less_timestamps(
    field_path: tuple[str, ...],
    naive_value: object,
) -> None:
    """Match the shipped TS parser: OpenAPI date-time requires a timezone offset."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    target: Any = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = naive_value
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_path", "non_rfc3339"),
    [
        (("generated_at",), "2026-09-07 12:00:00Z"),
        (("as_of",), "2026-09-07 12:00:00+00:00"),
        (("last_success_at",), "09/07/2026"),
        (("window", "start"), "2026-09-06"),
    ],
)
def test_dashboard_summary_rejects_non_rfc3339_timestamp_strings(
    field_path: tuple[str, ...],
    non_rfc3339: str,
) -> None:
    """Match the shipped TS parser: require RFC 3339 T separator before coercion."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    target: Any = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = non_rfc3339
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "coerced_value",
    ["1", True, 1.5, 1.0],
)
@pytest.mark.parametrize(
    "count_key",
    [
        "active",
        "executing",
        "monitoring_pr",
        "awaiting_operator",
        "awaiting_human",
        "retrying",
        "queued",
        "completed_last_window",
        "cancelled_last_window",
        "failed_last_window",
    ],
)
def test_dashboard_summary_rejects_coerced_count_values(
    count_key: str,
    coerced_value: object,
) -> None:
    """Match the shipped TS parser: counts must be JSON numbers (strict ints), not coerced."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["counts"][count_key] = coerced_value
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "coerced_value",
    ["false", "true", 0, 1],
)
@pytest.mark.parametrize(
    "overlap_key",
    [
        "awaiting_human_subset_of_monitoring_pr",
        "awaiting_operator_in_active_not_executing",
        "retrying_in_active_not_executing",
    ],
)
def test_dashboard_summary_rejects_coerced_overlap_flags(
    overlap_key: str,
    coerced_value: object,
) -> None:
    """Match the shipped TS parser: overlap flags must be JSON booleans, not coerced."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["overlap"][overlap_key] = coerced_value
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_normalizes_omitted_coverage_notes() -> None:
    """notes is optional in OpenAPI; omitted values become []."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    del payload["coverage"]["notes"]
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.coverage.notes == []


@pytest.mark.unit
@pytest.mark.parametrize("notes", [None, "x", [1], [{"text": "x"}], [True]])
def test_dashboard_summary_rejects_malformed_coverage_notes(notes: object) -> None:
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["notes"] = notes
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize("since_hours", [-1, 0, 1.5, "24", True])
def test_dashboard_summary_rejects_non_positive_since_hours(since_hours: object) -> None:
    """Match the shipped TS parser: window.since_hours must be a positive integer."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["window"]["since_hours"] = since_hours
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_accepts_positive_integer_since_hours() -> None:
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["window"]["since_hours"] = 1
    payload["window"]["start"] = "2026-09-06T16:00:00Z"
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.window.since_hours == 1


@pytest.mark.unit
def test_dashboard_summary_rejects_window_start_mismatching_since_hours() -> None:
    """Match the shipped TS parser: window.start must equal generated_at - since_hours."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["window"]["start"] = "2026-09-01T17:00:00Z"
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)

    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["window"]["start"] = "2026-09-05T16:00:00Z"
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)

    payload = copy.deepcopy(_dashboard_summary_payload())
    # Same absolute instant as generated_at - 24h via a non-Z offset.
    payload["window"]["start"] = "2026-09-05T10:00:00-07:00"
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.window.start == datetime(2026, 9, 5, 17, 0, tzinfo=UTC)

    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["generated_at"] = "2026-09-06T17:00:00.250Z"
    payload["as_of"] = "2026-09-06T17:00:00.250Z"
    payload["last_success_at"] = "2026-09-06T17:00:00.250Z"
    payload["window"]["start"] = "2026-09-05T17:00:00.250Z"
    assert ConsoleDashboardSummaryResponse.model_validate(payload).window.start == datetime(
        2026, 9, 5, 17, 0, 0, 250000, tzinfo=UTC
    )

    payload["window"]["start"] = "2026-09-05T17:00:00.000Z"
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "counts_patch,overlap_patch",
    [
        ({"active": 1, "executing": 2}, {}),
        # Isolate monitoring_pr > active (fixture still has executing=4 unless reset).
        (
            {
                "active": 1,
                "executing": 0,
                "monitoring_pr": 2,
                "awaiting_operator": 0,
                "awaiting_human": 0,
                "retrying": 0,
                "queued": 0,
            },
            {},
        ),
        (
            {"monitoring_pr": 1, "awaiting_human": 2},
            {"awaiting_human_subset_of_monitoring_pr": True},
        ),
        # awaiting_operator alone exceeds active.
        (
            {
                "active": 1,
                "executing": 0,
                "monitoring_pr": 0,
                "awaiting_operator": 2,
                "awaiting_human": 0,
                "retrying": 0,
                "queued": 0,
            },
            {"awaiting_operator_in_active_not_executing": True},
        ),
        (
            {
                "active": 2,
                "executing": 2,
                "monitoring_pr": 0,
                "awaiting_operator": 1,
                "awaiting_human": 0,
                "retrying": 0,
                "queued": 0,
            },
            {"awaiting_operator_in_active_not_executing": True},
        ),
        # retrying alone exceeds active.
        (
            {
                "active": 1,
                "executing": 0,
                "monitoring_pr": 0,
                "awaiting_operator": 0,
                "awaiting_human": 0,
                "retrying": 2,
                "queued": 0,
            },
            {"retrying_in_active_not_executing": True},
        ),
        (
            {
                "active": 2,
                "executing": 2,
                "monitoring_pr": 0,
                "awaiting_operator": 0,
                "awaiting_human": 0,
                "retrying": 1,
                "queued": 0,
            },
            {"retrying_in_active_not_executing": True},
        ),
        # Combined disjoint buckets exceed active even though each pairwise check passes.
        (
            {
                "active": 3,
                "executing": 1,
                "monitoring_pr": 1,
                "awaiting_operator": 1,
                "awaiting_human": 0,
                "retrying": 1,
                "queued": 0,
            },
            {
                "awaiting_operator_in_active_not_executing": True,
                "retrying_in_active_not_executing": True,
            },
        ),
        (
            {
                "active": 2,
                "executing": 2,
                "monitoring_pr": 1,
                "awaiting_human": 0,
                "awaiting_operator": 0,
                "retrying": 0,
                "queued": 0,
            },
            {},
        ),
        # queued (requested) ⊆ active and is disjoint from executing.
        (
            {
                "active": 1,
                "executing": 0,
                "monitoring_pr": 0,
                "awaiting_operator": 0,
                "awaiting_human": 0,
                "retrying": 0,
                "queued": 2,
            },
            {},
        ),
        (
            {
                "active": 1,
                "executing": 1,
                "monitoring_pr": 0,
                "awaiting_operator": 0,
                "awaiting_human": 0,
                "retrying": 0,
                "queued": 1,
            },
            {},
        ),
        # Hosted golden regression: pre-fix active=12 cannot hold
        # executing(7)+monitoring_pr(3)+queued(4)+retrying(1)=15.
        (
            {
                "active": 12,
                "executing": 7,
                "monitoring_pr": 3,
                "awaiting_operator": 0,
                "awaiting_human": 2,
                "retrying": 1,
                "queued": 4,
            },
            {
                "awaiting_operator_in_active_not_executing": True,
                "retrying_in_active_not_executing": True,
            },
        ),
    ],
)
def test_dashboard_summary_rejects_contradictory_count_subsets(
    counts_patch: dict[str, int],
    overlap_patch: dict[str, bool],
) -> None:
    """Match the shipped TS parser: declared subset relationships must hold."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["counts"].update(counts_patch)
    payload["overlap"].update(overlap_patch)
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_rejects_complete_coverage_with_null_counts() -> None:
    """Match the shipped TS parser: complete coverage forbids null fleet counters."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["counts"]["queued"] = None
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)

    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["status"] = "partial"
    payload["coverage"]["notes"] = ["queued_count_unavailable"]
    payload["counts"]["queued"] = None
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.coverage.status == "partial"
    assert model.counts.queued is None

    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["status"] = "unknown"
    payload["counts"]["active"] = None
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.coverage.status == "unknown"
    assert model.counts.active is None


@pytest.mark.unit
@pytest.mark.parametrize("coverage_status", ["partial", "unknown"])
def test_dashboard_summary_allows_null_last_success_at_without_prior_success(
    coverage_status: str,
) -> None:
    """last_success_at is required but null until a fully successful snapshot exists."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["status"] = coverage_status
    payload["coverage"]["notes"] = ["no_prior_successful_snapshot"]
    if coverage_status == "partial":
        payload["counts"]["queued"] = None
    payload["last_success_at"] = None
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.last_success_at is None
    assert model.coverage.status == coverage_status
    dumped = model.model_dump(mode="json")
    assert "last_success_at" in dumped
    assert dumped["last_success_at"] is None


@pytest.mark.unit
def test_dashboard_summary_rejects_omitted_last_success_at() -> None:
    """Keep last_success_at required so providers cannot drop the provenance key."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    del payload["last_success_at"]
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_rejects_null_last_success_at_when_coverage_complete() -> None:
    """A complete snapshot is itself a successful build and must name last_success_at."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["last_success_at"] = None
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)


@pytest.mark.unit
def test_dashboard_summary_skips_subset_checks_for_null_counts() -> None:
    """Null related counts skip subset checks; unavailable data uses partial coverage."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["status"] = "partial"
    payload["counts"]["active"] = None
    payload["counts"]["executing"] = 5
    assert ConsoleDashboardSummaryResponse.model_validate(payload).counts.executing == 5

    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["coverage"]["status"] = "partial"
    payload["coverage"]["notes"] = ["awaiting_human_unavailable"]
    payload["counts"]["monitoring_pr"] = 1
    payload["counts"]["awaiting_human"] = None
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.counts.awaiting_human is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "overlap_key",
    [
        "awaiting_human_subset_of_monitoring_pr",
        "awaiting_operator_in_active_not_executing",
        "retrying_in_active_not_executing",
    ],
)
def test_dashboard_summary_rejects_false_overlap_flags(overlap_key: str) -> None:
    """v1 overlap flags are fixed invariants (literal true), not provider toggles."""
    payload = copy.deepcopy(_dashboard_summary_payload())
    payload["overlap"][overlap_key] = False
    with pytest.raises(ValidationError):
        ConsoleDashboardSummaryResponse.model_validate(payload)
