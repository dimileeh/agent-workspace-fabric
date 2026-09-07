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
def test_available_widget_diagnostic_route_rule_matches_openapi_and_pydantic() -> None:
    """OpenAPI Draft202012 and Pydantic must agree on available-route enforcement."""
    openapi_validator = _console_capabilities_openapi_validator()
    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"]
    capabilities_schema = schema["ConsoleCapabilitiesResponse"]
    for collection in ("widgets", "diagnostics"):
        items = capabilities_schema["properties"][collection]["items"]
        assert "allOf" in items, (
            f"published {collection} items must wrap $ref + available⇒route in allOf"
        )
        route_constraint = next(
            (part for part in items["allOf"] if isinstance(part, dict) and "if" in part),
            None,
        )
        assert route_constraint is not None and "then" in route_constraint, (
            f"published {collection} items must encode available⇒route via if/then"
        )
        assert "route" in route_constraint["then"].get("required", [])
        route_schema = route_constraint["then"]["properties"]["route"]
        assert route_schema.get("type") == "string"
        assert route_schema.get("pattern", "").startswith("^/v1/")

    controls_items = capabilities_schema["properties"]["controls"]["items"]
    assert "allOf" not in controls_items and "if" not in controls_items, (
        "controls must not require route when available"
    )

    base = _local_capabilities_payload()
    assert _pydantic_accepts(base) is True
    assert _openapi_accepts(openapi_validator, base) is True

    for collection in ("widgets", "diagnostics"):
        bad = copy.deepcopy(base)
        for item in bad[collection]:
            if item["availability"] == "available":
                item.pop("route", None)
                break
        assert _pydantic_accepts(bad) is False, f"pydantic should reject {collection}"
        assert _openapi_accepts(openapi_validator, bad) is False, (
            f"openapi should reject {collection}"
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
