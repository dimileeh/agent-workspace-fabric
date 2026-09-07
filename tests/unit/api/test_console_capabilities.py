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

_CAPABILITIES_BASE: dict[str, Any] = {
    "schema_version": 1,
    "backend_kind": "hosted",
    "generated_at": "2026-09-06T17:00:00Z",
    "widgets": [],
    "diagnostics": [],
    "controls": [],
}

# Identical payloads checked against both Pydantic and committed OpenAPI.
_HOSTED_IDENTITY_NEGATIVE_PAYLOADS: dict[str, dict[str, Any]] = {
    "hosted_identity_omitted": dict(_CAPABILITIES_BASE),
    "hosted_identity_null": {**_CAPABILITIES_BASE, "identity": None},
    "hosted_tenant_id_omitted": {
        **_CAPABILITIES_BASE,
        "identity": {"backend_id": "awf-cloud", "scope": "tenant"},
    },
    "hosted_tenant_id_null": {
        **_CAPABILITIES_BASE,
        "identity": {
            "backend_id": "awf-cloud",
            "scope": "tenant",
            "tenant_id": None,
        },
    },
    "hosted_tenant_id_empty": {
        **_CAPABILITIES_BASE,
        "identity": {
            "backend_id": "awf-cloud",
            "scope": "tenant",
            "tenant_id": "",
        },
    },
}

_HOSTED_IDENTITY_POSITIVE_PAYLOADS: dict[str, dict[str, Any]] = {
    "hosted_complete_identity": {
        **_CAPABILITIES_BASE,
        "identity": {
            "backend_id": "awf-cloud",
            "scope": "tenant",
            "tenant_id": "tenant_a",
        },
    },
    "local_identity_omitted": {
        "schema_version": 1,
        "backend_kind": "local",
        "generated_at": "2026-09-06T17:00:00Z",
        "widgets": [],
        "diagnostics": [],
        "controls": [],
    },
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
    not certify hosted payloads that omit identity or leave tenant_id empty."""
    for payload in _HOSTED_IDENTITY_NEGATIVE_PAYLOADS.values():
        with pytest.raises(ValidationError):
            ConsoleCapabilitiesResponse.model_validate(payload)
    ok = ConsoleCapabilitiesResponse.model_validate(
        _HOSTED_IDENTITY_POSITIVE_PAYLOADS["hosted_complete_identity"]
    )
    assert ok.identity is not None
    assert ok.identity.tenant_id == "tenant_a"


@pytest.mark.unit
def test_hosted_identity_rule_matches_between_openapi_and_pydantic() -> None:
    """Committed OpenAPI must machine-enforce the same hosted identity rule as Pydantic.

    Draft202012 against openapi.json and ConsoleCapabilitiesResponse.model_validate
    must agree on the five hosted negatives and the local/hosted positives.
    """
    openapi_validator = _console_capabilities_openapi_validator()
    schema = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))["components"]["schemas"][
        "ConsoleCapabilitiesResponse"
    ]
    assert "if" in schema and "then" in schema, (
        "published ConsoleCapabilitiesResponse must encode hosted identity via if/then"
    )

    for name, payload in _HOSTED_IDENTITY_NEGATIVE_PAYLOADS.items():
        assert not _pydantic_accepts(payload), f"pydantic should reject {name}"
        assert not _openapi_accepts(openapi_validator, payload), (
            f"openapi Draft202012 should reject {name}"
        )

    for name, payload in _HOSTED_IDENTITY_POSITIVE_PAYLOADS.items():
        assert _pydantic_accepts(payload), f"pydantic should accept {name}"
        assert _openapi_accepts(openapi_validator, payload), (
            f"openapi Draft202012 should accept {name}"
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
