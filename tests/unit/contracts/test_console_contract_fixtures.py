"""Contract fixture ↔ Pydantic parity for console backend schema_version=1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from awf.api.routes.console import (
    ConsoleCapabilitiesResponse,
    ConsoleDashboardSummaryResponse,
)

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "console" / "fixtures" / "v1"


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "capabilities.local.json",
        "capabilities.hosted.json",
    ],
)
def test_capabilities_fixtures_validate(name: str) -> None:
    payload = _load(name)
    model = ConsoleCapabilitiesResponse.model_validate(payload)
    assert model.schema_version == 1
    for item in [*model.widgets, *model.diagnostics]:
        if item.availability == "available":
            assert item.route is not None
            assert item.route.startswith("/v1/")
            assert "://" not in item.route


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "dashboard-summary.local.json",
        "dashboard-summary.hosted.json",
        "dashboard-summary.partial.json",
        "dashboard-summary.no-prior-success.json",
    ],
)
def test_dashboard_summary_fixtures_validate(name: str) -> None:
    payload = _load(name)
    model = ConsoleDashboardSummaryResponse.model_validate(payload)
    assert model.schema_version == 1
    if name.endswith("no-prior-success.json"):
        assert model.coverage.status == "partial"
        assert model.last_success_at is None
        dumped = model.model_dump(mode="json")
        assert "last_success_at" in dumped
        assert dumped["last_success_at"] is None
    if name.endswith("partial.json"):
        assert model.coverage.status == "partial"
        assert model.counts.queued is None
        assert model.counts.cancelled_last_window is None
    if name.endswith("hosted.json"):
        # Queued joined the combined disjoint active-bucket check; keep the
        # published golden inside active so Cloud reuse does not fail closed.
        assert model.counts.active == 15
        assert model.counts.executing == 7
        assert model.counts.monitoring_pr == 3
        assert model.counts.queued == 4
        assert model.counts.retrying == 1
        assert (
            model.counts.executing
            + model.counts.monitoring_pr
            + model.counts.queued
            + model.counts.retrying
            <= model.counts.active
        )


@pytest.mark.unit
def test_unknown_version_fixture_is_detectable() -> None:
    payload = _load("capabilities.unknown_version.json")
    assert payload["schema_version"] != 1  # type: ignore[index]


@pytest.mark.unit
def test_capabilities_response_rejects_non_v1_schema_version() -> None:
    payload = _load("capabilities.local.json")
    assert isinstance(payload, dict)
    for version in (2, 99):
        bad = {**payload, "schema_version": version}
        with pytest.raises(ValidationError):
            ConsoleCapabilitiesResponse.model_validate(bad)


@pytest.mark.unit
def test_dashboard_summary_response_rejects_non_v1_schema_version() -> None:
    payload = _load("dashboard-summary.local.json")
    assert isinstance(payload, dict)
    for version in (2, 99):
        bad = {**payload, "schema_version": version}
        with pytest.raises(ValidationError):
            ConsoleDashboardSummaryResponse.model_validate(bad)


@pytest.mark.unit
def test_malformed_capabilities_fixture_fails_validation() -> None:
    payload = _load("capabilities.malformed.json")
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(payload)


@pytest.mark.unit
def test_hosted_and_local_share_capabilities_schema() -> None:
    local = ConsoleCapabilitiesResponse.model_validate(_load("capabilities.local.json"))
    hosted = ConsoleCapabilitiesResponse.model_validate(_load("capabilities.hosted.json"))
    assert local.schema_version == hosted.schema_version == 1
    assert {w.id for w in local.widgets} == {w.id for w in hosted.widgets}
    assert local.backend_kind == "local"
    assert hosted.backend_kind == "hosted"


@pytest.mark.unit
def test_local_capabilities_allow_omitted_identity() -> None:
    payload = _load("capabilities.local.json")
    assert isinstance(payload, dict)
    without_identity = {k: v for k, v in payload.items() if k != "identity"}
    model = ConsoleCapabilitiesResponse.model_validate(without_identity)
    assert model.backend_kind == "local"
    assert model.identity is None


@pytest.mark.unit
def test_hosted_capabilities_require_nonempty_identity_fields() -> None:
    payload = _load("capabilities.hosted.json")
    assert isinstance(payload, dict)
    without_identity = {k: v for k, v in payload.items() if k != "identity"}
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(without_identity)

    for bad_identity in (
        None,
        {"backend_id": "", "scope": "tenant", "tenant_id": "t1"},
        {"backend_id": "awf-cloud", "scope": "", "tenant_id": "t1"},
        {"backend_id": "awf-cloud", "scope": "tenant", "tenant_id": None},
        {"backend_id": "awf-cloud", "scope": "tenant", "tenant_id": ""},
        {"backend_id": "awf-cloud", "scope": "tenant", "tenant_id": "   "},
    ):
        bad = {**payload, "identity": bad_identity}
        with pytest.raises(ValidationError):
            ConsoleCapabilitiesResponse.model_validate(bad)
