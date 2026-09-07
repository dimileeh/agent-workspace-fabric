"""Console capabilities API tests (schema_version=1)."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from awf.api.routes.console import ConsoleCapabilitiesResponse
from awf.common.config import get_settings

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "console" / "fixtures" / "v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_JSON = REPO_ROOT / "openapi.json"

_IDENTITY_MATRIX = json.loads(
    (FIXTURES / "capabilities.identity-matrix.json").read_text(encoding="utf-8")
)
_IDENTITY_MATRIX_CASES: list[dict[str, Any]] = _IDENTITY_MATRIX["cases"]
_HOSTED_IDENTITY_NEGATIVE_PAYLOADS: dict[str, dict[str, Any]] = {
    case["name"]: case["payload"]
    for case in _IDENTITY_MATRIX_CASES
    if case["expect"] == "reject" and str(case["name"]).startswith("hosted_")
}
_HOSTED_IDENTITY_POSITIVE_PAYLOADS: dict[str, dict[str, Any]] = {
    case["name"]: case["payload"]
    for case in _IDENTITY_MATRIX_CASES
    if case["expect"] == "accept" and case["name"] in {"hosted_complete", "local_identity_omitted"}
}
_ROUTE_MATRIX = json.loads(
    (FIXTURES / "capabilities.route-matrix.json").read_text(encoding="utf-8")
)
_ROUTE_MATRIX_CASES: list[dict[str, Any]] = _ROUTE_MATRIX["cases"]
_ROUTE_INVENTORY: dict[str, Any] = _ROUTE_MATRIX["inventory"]
_NEGATIVE_MATRIX = json.loads(
    (FIXTURES / "capabilities.negative-matrix.json").read_text(encoding="utf-8")
)
_NEGATIVE_MATRIX_CASES: list[dict[str, Any]] = _NEGATIVE_MATRIX["cases"]
_UNSUPPORTED_REASON_CODES: list[str] = _NEGATIVE_MATRIX["unsupported_reason_codes"]


def _rewrite_component_refs(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            assert isinstance(ref, str)
            assert ref.startswith("#/components/schemas/")
            name = ref.rsplit("/", 1)[-1]
            return {"$ref": f"urn:awf:schemas/{name}"}
        return {key: _rewrite_component_refs(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_component_refs(item) for item in obj]
    return obj


def _console_capabilities_openapi_validator() -> Draft202012Validator:
    """Draft202012 validator for committed ConsoleCapabilitiesResponse schema."""
    spec = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    rewritten = {
        f"urn:awf:schemas/{name}": Resource(
            contents=_rewrite_component_refs(schema),
            specification=DRAFT202012,
        )
        for name, schema in schemas.items()
    }
    registry = Registry().with_resources(rewritten.items())
    root = _rewrite_component_refs(copy.deepcopy(schemas["ConsoleCapabilitiesResponse"]))
    return Draft202012Validator(root, registry=registry)


def _openapi_accepts(validator: Draft202012Validator, payload: dict[str, Any]) -> bool:
    return not list(validator.iter_errors(payload))


def _pydantic_accepts(payload: dict[str, Any]) -> bool:
    try:
        ConsoleCapabilitiesResponse.model_validate(payload)
    except ValidationError:
        return False
    return True


@pytest.mark.unit
async def test_console_capabilities_requires_auth(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/console/capabilities",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["detail"]["error_code"] == "UNAUTHORIZED"
    assert "widgets" not in body
    assert "wrong-token" not in response.text


@pytest.mark.unit
async def test_console_capabilities_local_schema_v1(client: AsyncClient) -> None:
    response = await client.get("/v1/console/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["backend_kind"] == "local"
    assert body["identity"]["scope"] == "local"

    widgets = {item["id"]: item for item in body["widgets"]}
    assert widgets["fleet_summary"]["availability"] == "available"
    assert widgets["fleet_summary"]["route"] == "/v1/console/dashboard-summary"
    assert widgets["resource_capacity"]["availability"] == "available"
    assert widgets["resource_capacity"]["route"].startswith("/v1/")
    assert widgets["cloud_runtime"]["availability"] == "unsupported"
    assert widgets["cloud_runtime"]["reason_code"] == "backend_kind_local"
    assert widgets["cost"]["availability"] == "unsupported"
    assert widgets["cloud_runtime"].get("route") is None

    for group in ("widgets", "diagnostics", "controls"):
        for item in body[group]:
            route = item.get("route")
            if route is not None:
                assert route.startswith("/v1/")
                assert "://" not in route


@pytest.mark.unit
async def test_console_capabilities_matches_local_fixture_shape(client: AsyncClient) -> None:
    fixture = json.loads((FIXTURES / "capabilities.local.json").read_text(encoding="utf-8"))
    response = await client.get("/v1/console/capabilities")
    body = response.json()
    assert {w["id"] for w in body["widgets"]} == {w["id"] for w in fixture["widgets"]}
    assert {d["id"] for d in body["diagnostics"]} == {d["id"] for d in fixture["diagnostics"]}
    assert {c["id"] for c in body["controls"]} == {c["id"] for c in fixture["controls"]}
    live = {w["id"]: w["availability"] for w in body["widgets"]}
    expected = {w["id"]: w["availability"] for w in fixture["widgets"]}
    assert live == expected


@pytest.mark.unit
def test_hosted_capabilities_response_model_rejects_incomplete_identity() -> None:
    """Cloud implementers validating against the shared OpenAPI/Pydantic model must
    not certify hosted payloads that omit identity or leave tenant_id empty/blank."""
    for payload in _HOSTED_IDENTITY_NEGATIVE_PAYLOADS.values():
        with pytest.raises(ValidationError):
            ConsoleCapabilitiesResponse.model_validate(payload)
    ok = ConsoleCapabilitiesResponse.model_validate(
        _HOSTED_IDENTITY_POSITIVE_PAYLOADS["hosted_complete"]
    )
    assert ok.identity is not None
    assert ok.identity.tenant_id == "tenant_a"


def _local_capabilities_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "capabilities.local.json").read_text(encoding="utf-8"))


@pytest.mark.unit
@pytest.mark.parametrize("numeric_ts", [0, 1, 1_694_000_000, 1_694_000_000.5])
def test_capabilities_response_rejects_numeric_generated_at(numeric_ts: float) -> None:
    """Match the shipped TS parser: generated_at must be an ISO string, not Unix epoch."""
    payload = _local_capabilities_payload()
    payload["generated_at"] = numeric_ts
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(payload)


@pytest.mark.unit
def test_capabilities_response_accepts_iso_generated_at_string() -> None:
    payload = _local_capabilities_payload()
    assert isinstance(payload["generated_at"], str)
    model = ConsoleCapabilitiesResponse.model_validate(payload)
    assert model.generated_at.year == 2026
    assert model.generated_at.tzinfo is not None
    assert model.generated_at.utcoffset() is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "naive_value",
    [
        "2026-09-07T12:00:00",
        "2026-09-07T12:00:00.123456",
        datetime(2026, 9, 7, 12, 0, 0),
    ],
)
def test_capabilities_response_rejects_timezone_less_generated_at(
    naive_value: object,
) -> None:
    """Match the shipped TS parser: generated_at must include a timezone offset."""
    payload = _local_capabilities_payload()
    payload["generated_at"] = naive_value
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "non_rfc3339",
    [
        "2026-09-07 12:00:00Z",
        "2026-09-07 12:00:00+00:00",
        "09/07/2026",
        "2026-09-07",
    ],
)
def test_capabilities_response_rejects_non_rfc3339_generated_at(
    non_rfc3339: str,
) -> None:
    """Match the shipped TS parser: reject space-separated / non-RFC 3339 forms."""
    payload = _local_capabilities_payload()
    payload["generated_at"] = non_rfc3339
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "2026-09-07T12:00:00Z",
        "2026-09-07t12:00:00z",
        "2026-09-07T12:00:00.123+00:00",
        "2026-09-07T12:00:00-05:30",
    ],
)
def test_capabilities_response_accepts_rfc3339_generated_at_case_variants(
    value: str,
) -> None:
    payload = _local_capabilities_payload()
    payload["generated_at"] = value
    model = ConsoleCapabilitiesResponse.model_validate(payload)
    assert model.generated_at.tzinfo is not None
    assert model.generated_at.utcoffset() is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "invalid",
    [
        "2026-02-29T12:00:00Z",
        "2026-09-07T12:00:00+24:00",
        "2026-09-07T12:00:00+00:60",
    ],
)
def test_capabilities_response_rejects_impossible_rfc3339_generated_at(
    invalid: str,
) -> None:
    """Reject calendar-impossible dates and out-of-range offsets (TS parity)."""
    payload = _local_capabilities_payload()
    payload["generated_at"] = invalid
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(payload)


@pytest.mark.unit
def test_available_widgets_and_diagnostics_require_route_in_response_model() -> None:
    """Shared model must reject available widgets/diagnostics with route omitted/null.

    Controls correctly omit route when available; unsupported entries omit route.
    """
    base = _local_capabilities_payload()

    for collection in ("widgets", "diagnostics"):
        for missing in ("omit", "null"):
            bad = copy.deepcopy(base)
            for item in bad[collection]:
                if item["availability"] != "available":
                    continue
                if missing == "omit":
                    item.pop("route", None)
                else:
                    item["route"] = None
                break
            with pytest.raises(ValidationError):
                ConsoleCapabilitiesResponse.model_validate(bad)

    # Available controls omit route by contract — still valid.
    ok = ConsoleCapabilitiesResponse.model_validate(base)
    assert all(c.route is None for c in ok.controls if c.availability == "available")

    # Unsupported widget without route remains valid.
    unsupported = copy.deepcopy(base)
    for item in unsupported["widgets"]:
        if item["availability"] == "unsupported":
            item.pop("route", None)
            break
    ConsoleCapabilitiesResponse.model_validate(unsupported)


@pytest.mark.unit
def test_available_controls_must_omit_route() -> None:
    """Available controls with any non-null route must fail closed (TS parity).

    Mutations use hardcoded operatorActionPath and ignore advertised control
    routes, so certifying a relative ``/v1/...``, empty string, or absolute URL
    on an available control would advertise an endpoint that is never called.
    Absent and explicit null retain the documented omit semantics.
    """
    openapi_validator = _console_capabilities_openapi_validator()
    base = _local_capabilities_payload()

    relative = copy.deepcopy(base)
    for item in relative["controls"]:
        if item["availability"] == "available" and item["id"] == "retry":
            item["route"] = "/v1/workspaces/{workspace_id}/retry"
            break
    else:
        relative["controls"] = [
            {
                "id": "retry",
                "availability": "available",
                "route": "/v1/workspaces/{workspace_id}/retry",
                "semantics": "retry",
            }
        ]
    with pytest.raises(ValidationError, match="omit route"):
        ConsoleCapabilitiesResponse.model_validate(relative)
    assert openapi_validator.is_valid(relative) is False

    empty = copy.deepcopy(base)
    for item in empty["controls"]:
        if item["availability"] == "available" and item["id"] == "cancel":
            item["route"] = ""
            break
    else:
        empty["controls"] = [
            {
                "id": "cancel",
                "availability": "available",
                "route": "",
                "semantics": "cancel",
            }
        ]
    # Empty string must fail the omit-route contract (not only relative-route).
    with pytest.raises(ValidationError, match="omit route"):
        ConsoleCapabilitiesResponse.model_validate(empty)
    assert openapi_validator.is_valid(empty) is False

    absolute = copy.deepcopy(base)
    for item in absolute["controls"]:
        if item["availability"] == "available" and item["id"] == "refresh":
            item["route"] = "https://example.invalid/v1/workspaces/ws/refresh"
            break
    else:
        absolute["controls"] = [
            {
                "id": "refresh",
                "availability": "available",
                "route": "https://example.invalid/v1/workspaces/ws/refresh",
                "semantics": "refresh",
            }
        ]
    with pytest.raises(ValidationError, match="omit route"):
        ConsoleCapabilitiesResponse.model_validate(absolute)
    assert openapi_validator.is_valid(absolute) is False

    omitted = copy.deepcopy(base)
    for item in omitted["controls"]:
        item.pop("route", None)
    assert ConsoleCapabilitiesResponse.model_validate(omitted)
    assert openapi_validator.is_valid(omitted) is True

    explicit_null = copy.deepcopy(base)
    for item in explicit_null["controls"]:
        item["route"] = None
    validated = ConsoleCapabilitiesResponse.model_validate(explicit_null)
    assert openapi_validator.is_valid(explicit_null) is True

    # Non-list controls still fail closed after the omit-route pre-check.
    not_a_list = copy.deepcopy(base)
    not_a_list["controls"] = "retry"
    with pytest.raises(ValidationError):
        ConsoleCapabilitiesResponse.model_validate(not_a_list)

    class _RoutedControl:
        def __init__(self, route: object) -> None:
            self.route = route

    object_route = copy.deepcopy(base)
    object_route["controls"] = [_RoutedControl("/v1/workspaces/ws/retry")]
    with pytest.raises(ValidationError, match="omit route"):
        ConsoleCapabilitiesResponse.model_validate(object_route)

    # After-validator remains the second omit-route gate if a control route is set.
    validated.controls[0].route = "/v1/workspaces/{workspace_id}/retry"
    with pytest.raises(ValueError, match="omit route"):
        validated.controls_must_omit_route()


@pytest.mark.unit
def test_unsupported_capability_entries_must_omit_route() -> None:
    """Unsupported widgets/diagnostics with any route must fail closed (TS parity).

    A relative ``/v1/wrong-route`` (or even the inventory route) must not certify
    through Pydantic/OpenAPI while parseConsoleCapabilities rejects route-on-
    unsupported and disables negotiation.
    """
    openapi_validator = _console_capabilities_openapi_validator()
    base = _local_capabilities_payload()

    for collection, item_id, wrong_route in (
        ("widgets", "fleet_summary", "/v1/wrong-route"),
        ("diagnostics", "reliability", "/v1/wrong-route"),
        ("widgets", "cloud_runtime", "/v1/console/cloud-runtime"),
    ):
        bad = copy.deepcopy(base)
        # Replace collection with a single unsupported entry carrying a route.
        reason = "policy_disabled" if collection == "widgets" else "backend_kind_local"
        bad[collection] = [
            {
                "id": item_id,
                "availability": "unsupported",
                "reason_code": reason,
                "message": "withdrawn",
                "semantics": item_id,
                "route": wrong_route,
            }
        ]
        if collection == "widgets":
            # Keep remaining inventory valid so only the unsupported+route fails.
            bad["diagnostics"] = [
                item for item in base["diagnostics"] if item["availability"] == "available"
            ][:1] or base["diagnostics"][:1]
        with pytest.raises(ValidationError, match="omit route"):
            ConsoleCapabilitiesResponse.model_validate(bad)
        assert openapi_validator.is_valid(bad) is False

    item_schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"][
        "ConsoleCapabilityItemResponse"
    ]
    assert item_schema["if"]["properties"]["availability"]["const"] == "unsupported"
    assert item_schema["then"]["properties"]["route"] == {"type": "null"}


@pytest.mark.unit
def test_available_widget_diagnostic_exact_inventory_routes_match_openapi_and_pydantic() -> None:
    """OpenAPI Draft202012 and Pydantic must agree on exact inventory routes by id.

    A relative `/v1/wrong-route` must not certify an available fleet_summary or
    reliability entry — the shipped console rejects non-inventory routes.
    Available ``unknown_audit_id`` with any `/v1/...` route must also fail closed
    (OpenAPI id enum matches Python ``inventory.get`` rejection).
    """
    openapi_validator = _console_capabilities_openapi_validator()
    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"]
    capabilities_schema = schema["ConsoleCapabilitiesResponse"]
    widget_routes: dict[str, str] = _ROUTE_INVENTORY["widgets"]
    diagnostic_routes: dict[str, str] = _ROUTE_INVENTORY["diagnostics"]
    widgets_without_route: list[str] = _ROUTE_INVENTORY["widgets_without_route"]

    for collection, inventory in (
        ("widgets", widget_routes),
        ("diagnostics", diagnostic_routes),
    ):
        items = capabilities_schema["properties"][collection]["items"]
        assert "allOf" in items, (
            f"published {collection} items must wrap $ref + route rules in allOf"
        )
        available_gate = next(
            part
            for part in items["allOf"]
            if isinstance(part, dict)
            and "if" in part
            and isinstance(part.get("if"), dict)
            and part["if"].get("properties", {}).get("availability", {}).get("const") == "available"
            and "id" not in part["if"].get("properties", {})
        )
        assert available_gate["then"]["properties"]["id"]["enum"] == sorted(inventory), (
            f"published {collection} available entries must require an inventory id "
            f"(matching Python inventory.get rejection)"
        )
        assert "id" in available_gate["then"].get("required", [])
        id_route_constraints = [
            part
            for part in items["allOf"]
            if isinstance(part, dict)
            and "if" in part
            and isinstance(part.get("if"), dict)
            and "id" in part["if"].get("properties", {})
            and part["if"]["properties"]["id"].get("const") in inventory
        ]
        encoded_ids = {part["if"]["properties"]["id"]["const"] for part in id_route_constraints}
        assert encoded_ids == set(inventory), (
            f"published {collection} items must encode exact const routes for every "
            f"inventory id; missing={set(inventory) - encoded_ids}"
        )
        for part in id_route_constraints:
            item_id = part["if"]["properties"]["id"]["const"]
            assert part["if"]["properties"]["availability"]["const"] == "available"
            assert part["then"]["properties"]["route"]["const"] == inventory[item_id]
            assert "route" in part["then"].get("required", [])

    widget_items = capabilities_schema["properties"]["widgets"]["items"]
    no_route_constraints = [
        part
        for part in widget_items["allOf"]
        if isinstance(part, dict)
        and "if" in part
        and isinstance(part.get("if"), dict)
        and part["if"].get("properties", {}).get("id", {}).get("const") in widgets_without_route
    ]
    assert {part["if"]["properties"]["id"]["const"] for part in no_route_constraints} == set(
        widgets_without_route
    ), "widgets without inventory routes must be OpenAPI-forbidden when available"

    controls_items = capabilities_schema["properties"]["controls"]["items"]
    assert "allOf" in controls_items, "controls items must wrap $ref + id bound in allOf"
    control_id_bounds = [
        part
        for part in controls_items["allOf"]
        if isinstance(part, dict)
        and "properties" in part
        and "id" in part.get("properties", {})
        and "enum" in part["properties"]["id"]
        and "if" not in part
    ]
    assert len(control_id_bounds) == 1
    assert control_id_bounds[0]["properties"]["id"]["enum"] == sorted(
        _ROUTE_INVENTORY["controls"]
    ), "published controls must bound id to the control inventory for all availability states"
    assert not any(
        isinstance(part, dict)
        and "if" in part
        and part.get("then") not in (False, None)
        and "route" in (part.get("then") or {}).get("properties", {})
        and part.get("then", {}).get("properties", {}).get("route", {}).get("const") is not None
        for part in controls_items["allOf"]
    ), "controls must not require a non-null inventory route when available"
    assert any(
        isinstance(part, dict) and part.get("properties", {}).get("route") == {"type": "null"}
        for part in controls_items["allOf"]
    ), "published controls must encode omit-route (route type null) for all availability states"

    base = _local_capabilities_payload()
    assert _pydantic_accepts(base) is True
    assert _openapi_accepts(openapi_validator, base) is True

    for case in _ROUTE_MATRIX_CASES:
        name = case["name"]
        payload = case["payload"]
        expect_accept = case["expect"] == "accept"
        assert _pydantic_accepts(payload) is expect_accept, (
            f"pydantic should {'accept' if expect_accept else 'reject'} {name}"
        )
        assert _openapi_accepts(openapi_validator, payload) is expect_accept, (
            f"openapi Draft202012 should {'accept' if expect_accept else 'reject'} {name}"
        )


@pytest.mark.unit
def test_identity_tenant_nonblank_matrix_matches_openapi_and_pydantic() -> None:
    """Shared fixture matrix: OpenAPI Draft202012 and Pydantic must agree.

    Encodes the Python strip()-nonblank tenant_id rule (spaces/tabs/newlines),
    hosted completeness, local optional/null identity, and wrong-type rejects.
    """
    openapi_validator = _console_capabilities_openapi_validator()
    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"]
    capabilities_schema = schema["ConsoleCapabilitiesResponse"]
    identity_schema = schema["ConsoleCapabilitiesIdentityResponse"]
    assert "if" in capabilities_schema and "then" in capabilities_schema, (
        "published ConsoleCapabilitiesResponse must encode hosted identity via if/then"
    )
    hosted_tenant = capabilities_schema["then"]["properties"]["identity"]["properties"]["tenant_id"]
    assert hosted_tenant.get("pattern") == r".*\S.*", (
        "hosted if/then tenant_id must encode nonblank (not mere minLength)"
    )
    tenant_any_of = identity_schema["properties"]["tenant_id"]["anyOf"]
    string_branch = next(branch for branch in tenant_any_of if branch.get("type") == "string")
    assert string_branch.get("pattern") == r".*\S.*", (
        "identity.tenant_id string branch must encode nonblank when provided"
    )

    for case in _IDENTITY_MATRIX_CASES:
        name = case["name"]
        payload = case["payload"]
        expect_accept = case["expect"] == "accept"
        assert _pydantic_accepts(payload) is expect_accept, (
            f"pydantic should {'accept' if expect_accept else 'reject'} {name}"
        )
        assert _openapi_accepts(openapi_validator, payload) is expect_accept, (
            f"openapi Draft202012 should {'accept' if expect_accept else 'reject'} {name}"
        )


@pytest.mark.unit
def test_capability_item_rejects_empty_semantics() -> None:
    """Shared model must reject empty semantics the shipped TS parser already rejects."""
    openapi_validator = _console_capabilities_openapi_validator()
    payload = _local_capabilities_payload()
    payload["widgets"][0]["semantics"] = ""
    assert _pydantic_accepts(payload) is False
    assert _openapi_accepts(openapi_validator, payload) is False
    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"]
    semantics = schema["ConsoleCapabilityItemResponse"]["properties"]["semantics"]
    assert semantics.get("minLength") == 1


@pytest.mark.unit
def test_capability_ids_known_and_unique_for_all_availability_states() -> None:
    """Shared model must reject unknown/duplicate ids for unsupported entries and controls.

    Available-only inventory gates are insufficient: an unsupported unknown widget,
    unknown control, or repeated remonitor id must fail closed for Cloud implementers
    validating against ConsoleCapabilitiesResponse / OpenAPI.
    """
    openapi_validator = _console_capabilities_openapi_validator()
    base = _local_capabilities_payload()

    unsupported_unknown = copy.deepcopy(base)
    unsupported_unknown["widgets"] = [
        {
            "id": "unknown_audit_id",
            "availability": "unsupported",
            "reason_code": "not_implemented",
            "message": "unknown",
            "semantics": "audit",
        }
    ]
    assert _pydantic_accepts(unsupported_unknown) is False
    assert _openapi_accepts(openapi_validator, unsupported_unknown) is False

    unknown_control = copy.deepcopy(base)
    unknown_control["controls"] = [
        {
            "id": "explode",
            "availability": "available",
            "semantics": "explode",
        }
    ]
    assert _pydantic_accepts(unknown_control) is False
    assert _openapi_accepts(openapi_validator, unknown_control) is False

    duplicate_control = copy.deepcopy(base)
    duplicate_control["controls"] = [
        {"id": "retry", "availability": "available", "semantics": "retry"},
        {
            "id": "retry",
            "availability": "unsupported",
            "semantics": "retry",
            "reason_code": "policy_disabled",
            "message": "dup",
        },
    ]
    assert _pydantic_accepts(duplicate_control) is False
    # OpenAPI Draft202012 encodes per-id uniqueness via contains/maxContains=1.
    assert _openapi_accepts(openapi_validator, duplicate_control) is False
    assert _pydantic_accepts(base) is True
    assert _openapi_accepts(openapi_validator, base) is True

    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"]
    capabilities_schema = schema["ConsoleCapabilitiesResponse"]
    for collection, expected_ids in (
        (
            "widgets",
            sorted(
                set(_ROUTE_INVENTORY["widgets"]) | set(_ROUTE_INVENTORY["widgets_without_route"])
            ),
        ),
        ("diagnostics", sorted(_ROUTE_INVENTORY["diagnostics"])),
        ("controls", sorted(_ROUTE_INVENTORY["controls"])),
    ):
        items = capabilities_schema["properties"][collection]["items"]
        assert "allOf" in items
        id_bounds = [
            part
            for part in items["allOf"]
            if isinstance(part, dict)
            and "if" not in part
            and isinstance(part.get("properties"), dict)
            and "id" in part["properties"]
            and "enum" in part["properties"]["id"]
        ]
        assert len(id_bounds) == 1, f"{collection} must publish unconditional id enum"
        assert id_bounds[0]["properties"]["id"]["enum"] == expected_ids
        uniqueness = capabilities_schema["properties"][collection].get("allOf")
        assert isinstance(uniqueness, list), f"{collection} must publish per-id maxContains"
        assert len(uniqueness) == len(expected_ids)
        for constraint, item_id in zip(uniqueness, expected_ids, strict=True):
            assert constraint["maxContains"] == 1
            assert constraint["minContains"] == 0
            assert constraint["contains"]["properties"]["id"]["const"] == item_id


@pytest.mark.unit
def test_negative_matrix_matches_openapi_and_pydantic() -> None:
    """Shared unsupported-reason + duplicate-id negatives agree across surfaces."""
    openapi_validator = _console_capabilities_openapi_validator()
    item_schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"][
        "ConsoleCapabilityItemResponse"
    ]
    assert item_schema.get("if", {}).get("properties", {}).get("availability", {}).get("const") == (
        "unsupported"
    )
    then_props = item_schema["then"]["properties"]
    assert then_props["reason_code"]["enum"] == sorted(_UNSUPPORTED_REASON_CODES)
    assert then_props["message"]["minLength"] == 1

    for case in _NEGATIVE_MATRIX_CASES:
        name = case["name"]
        payload = case["payload"]
        expect_accept = case["expect"] == "accept"
        assert _pydantic_accepts(payload) is expect_accept, (
            f"pydantic should {'accept' if expect_accept else 'reject'} {name}"
        )
        assert _openapi_accepts(openapi_validator, payload) is expect_accept, (
            f"openapi Draft202012 should {'accept' if expect_accept else 'reject'} {name}"
        )


@pytest.mark.unit
async def test_console_capabilities_unconfigured_token_is_distinguishable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.get("/v1/console/capabilities")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "API_TOKEN_NOT_CONFIGURED"
