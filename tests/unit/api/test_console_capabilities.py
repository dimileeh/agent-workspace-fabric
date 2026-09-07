"""Console capabilities API tests (schema_version=1)."""

from __future__ import annotations

import copy
import json
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
        for part in controls_items["allOf"]
    ), "controls must not require route when available"

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
        {"id": "retry", "availability": "unsupported", "semantics": "retry", "reason_code": "x"},
    ]
    assert _pydantic_accepts(duplicate_control) is False
    # OpenAPI Draft202012 cannot encode per-property uniqueness; Pydantic is authoritative.
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
