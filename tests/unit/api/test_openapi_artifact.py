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

import awf.api.app as app_module
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
def test_callback_endpoints_expose_authorization_header_in_openapi(openapi_spec: dict) -> None:
    path = openapi_spec["paths"]["/v1/callbacks"]
    for method in ("get", "post"):
        operation = path[method]
        parameters = operation["parameters"]
        authorization_params = [
            param
            for param in parameters
            if param.get("in") == "header" and param.get("name") == "authorization"
        ]
        assert authorization_params, (
            f"{method.upper()} /v1/callbacks is expected to expose Authorization header"
        )
        assert all(param.get("required") is True for param in authorization_params), (
            f"{method.upper()} /v1/callbacks Authorization header must be required"
        )


@pytest.mark.unit
def test_openapi_auth_contract_patch_runs_once_after_schema_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = app_module.create_app(use_lifespan=False)
    marker_calls = 0
    original_marker = app_module._mark_authorization_header_parameters_required

    def recording_marker(
        openapi_schema: dict,
        auth_required_operations: set[tuple[str, str]],
    ) -> None:
        nonlocal marker_calls
        marker_calls += 1
        original_marker(openapi_schema, auth_required_operations)

    monkeypatch.setattr(
        app_module,
        "_mark_authorization_header_parameters_required",
        recording_marker,
    )

    first_schema = app.openapi()
    second_schema = app.openapi()

    assert first_schema is second_schema
    assert marker_calls == 1


@pytest.mark.unit
def test_callback_endpoints_document_structured_error_responses(
    openapi_spec: dict,
) -> None:
    path = openapi_spec["paths"]["/v1/callbacks"]
    expected_statuses_by_method = {
        "get": {"401", "503"},
        "post": {"400", "401", "409", "503"},
    }

    for method, expected_statuses in expected_statuses_by_method.items():
        responses = path[method]["responses"]
        assert expected_statuses <= responses.keys()
        assert "403" not in responses
        for status_code in expected_statuses:
            schema = responses[status_code]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/HTTPExceptionErrorResponse"}

    wrapper_schema = openapi_spec["components"]["schemas"]["HTTPExceptionErrorResponse"]
    assert wrapper_schema["required"] == ["detail"]
    assert wrapper_schema["properties"]["detail"] == {
        "$ref": "#/components/schemas/ErrorResponse",
    }


@pytest.mark.unit
def test_authorization_headers_are_required_in_openapi(openapi_spec: dict) -> None:
    optional_auth_headers: list[str] = []
    for path, path_item in openapi_spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, dict):
                    continue
                if (
                    parameter.get("in") == "header"
                    and parameter.get("name") == "authorization"
                    and parameter.get("required") is not True
                ):
                    optional_auth_headers.append(f"{method.upper()} {path}")

    assert optional_auth_headers == []


@pytest.mark.unit
def test_required_authorization_headers_are_non_nullable_strings_in_openapi(
    openapi_spec: dict,
) -> None:
    invalid_auth_headers: list[str] = []
    for path, path_item in openapi_spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, dict):
                    continue
                if (
                    parameter.get("in") != "header"
                    or parameter.get("name") != "authorization"
                    or parameter.get("required") is not True
                ):
                    continue
                schema = parameter.get("schema")
                if (
                    not isinstance(schema, dict)
                    or schema.get("type") != "string"
                    or schema.get("minLength") != 1
                    or "anyOf" in schema
                ):
                    invalid_auth_headers.append(f"{method.upper()} {path}")

    assert invalid_auth_headers == []


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
