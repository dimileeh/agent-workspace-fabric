"""Regression tests for the stable OpenAPI spec artifact.

These tests verify that:
1. The OpenAPI spec can be generated from the FastAPI app.
2. The generated spec is structurally valid OpenAPI 3.x.
3. All expected API path prefixes are present.
4. Key endpoint methods exist in the spec.
5. No duplicate operation IDs exist.
6. All response model schemas are present in components/schemas.
7. The spec round-trips through JSON serialization without error.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from awf.api.app import create_app


@pytest.fixture(scope="module")
def openapi_spec() -> dict:
    app = create_app(use_lifespan=False)
    return app.openapi()


@pytest.mark.unit
def test_spec_generation_succeeds(openapi_spec: dict) -> None:
    assert "openapi" in openapi_spec, "Spec missing 'openapi' key"
    assert "info" in openapi_spec, "Spec missing 'info' key"
    assert "paths" in openapi_spec, "Spec missing 'paths' key"
    assert openapi_spec["openapi"].startswith("3."), (
        f"Expected OpenAPI 3.x, got {openapi_spec['openapi']}"
    )


@pytest.mark.unit
def test_spec_is_valid_openapi_3x(openapi_spec: dict) -> None:
    from openapi_spec_validator import validate

    try:
        validate(openapi_spec)
    except Exception as exc:
        pytest.fail(f"OpenAPI spec validation failed: {exc}")


@pytest.mark.unit
def test_all_route_prefixes_present(openapi_spec: dict) -> None:
    expected_prefixes = [
        "/healthz",
        "/readyz",
        "/release-readiness",
        "/v1/workspaces",
        "/v2/workspaces",
        "/v1/events",
        "/v1/tasks",
        "/v1/callbacks",
        "/v1/merge-queue",
        "/v1/operations",
        "/v1/metrics",
        "/v1/locks",
    ]
    paths = set(openapi_spec.get("paths", {}).keys())
    for prefix in expected_prefixes:
        matching = [p for p in paths if p.startswith(prefix)]
        assert matching, (
            f"Expected path prefix {prefix!r} not found in spec paths. Available: {sorted(paths)}"
        )


@pytest.mark.unit
def test_key_endpoint_methods_exist(openapi_spec: dict) -> None:
    paths = openapi_spec.get("paths", {})
    expected_methods: list[tuple[str, str]] = [
        ("POST", "/v2/workspaces"),
        ("GET", "/v1/workspaces/{workspace_id}"),
        ("GET", "/v1/workspaces"),
        ("GET", "/v1/events"),
        ("GET", "/v1/workspaces/{workspace_id}/logs"),
        ("POST", "/v1/workspaces/{workspace_id}/validate"),
        ("POST", "/v1/workspaces/{workspace_id}/remonitor"),
        ("POST", "/v1/workspaces/{workspace_id}/retry"),
        ("GET", "/release-readiness"),
        ("POST", "/v1/workspaces/{workspace_id}/cancel"),
        ("DELETE", "/v1/workspaces/{workspace_id}"),
    ]
    for method, path in expected_methods:
        assert path in paths, (
            f"Expected path {path!r} not in spec. Available: {sorted(paths.keys())}"
        )
        path_item = paths[path]
        assert method.lower() in path_item, (
            f"Expected {method} {path} in spec, but only found methods: {list(path_item.keys())}"
        )


@pytest.mark.unit
def test_no_duplicate_operation_ids(openapi_spec: dict) -> None:
    operation_ids: list[str] = []
    for path_item in openapi_spec.get("paths", {}).values():
        for method_obj in path_item.values():
            if isinstance(method_obj, dict) and "operationId" in method_obj:
                operation_ids.append(method_obj["operationId"])
    counts = Counter(operation_ids)
    unique_duplicates = sorted(oid for oid, c in counts.items() if c > 1)
    assert not unique_duplicates, f"Duplicate operationIds found: {unique_duplicates}"


@pytest.mark.unit
def test_response_model_schemas_present(openapi_spec: dict) -> None:
    schemas = openapi_spec.get("components", {}).get("schemas", {})
    referenced_schemas: set[str] = set()
    for path_item in openapi_spec.get("paths", {}).values():
        for method_obj in path_item.values():
            if not isinstance(method_obj, dict):
                continue
            responses = method_obj.get("responses", {})
            for response_obj in responses.values():
                if not isinstance(response_obj, dict):
                    continue
                content = response_obj.get("content", {})
                for media_obj in content.values():
                    if not isinstance(media_obj, dict):
                        continue
                    ref = media_obj.get("schema", {}).get("$ref", "")
                    if ref.startswith("#/components/schemas/"):
                        schema_name = ref.split("/")[-1]
                        referenced_schemas.add(schema_name)
    missing = sorted(referenced_schemas - set(schemas.keys()))
    assert not missing, f"Referenced schemas missing from components/schemas: {missing}"


@pytest.mark.unit
def test_spec_round_trips_to_json_and_back(openapi_spec: dict) -> None:
    serialized = json.dumps(openapi_spec, sort_keys=True)
    deserialized = json.loads(serialized)
    assert deserialized == openapi_spec, "Spec changed during JSON round-trip"
