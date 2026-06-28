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
from httpx import ASGITransport, AsyncClient

from awf.api.app import create_app
from awf.common.config import get_settings

_WWW_AUTHENTICATE_HEADER = {
    "description": "Bearer challenge for the API token.",
    "schema": {"type": "string"},
}
_RETRY_AFTER_HEADER = {
    "description": (
        "Backoff value clients should use before retrying; either delta-seconds "
        "or an HTTP-date as defined by RFC 7231."
    ),
    "schema": {"type": "string"},
}
_HTTP_EXCEPTION_ERROR_RESPONSE_REF = "#/components/schemas/HttpExceptionErrorResponse"
_CALLBACK_HTTP_EXCEPTION_ERROR_RESPONSE_REF = "#/components/schemas/HTTPExceptionErrorResponse"
_ERROR_RESPONSE_REF = "#/components/schemas/ErrorResponse"
_RELEASE_READINESS_RESPONSE_REF = "#/components/schemas/ReleaseReadinessResponse"
_PUBLIC_PROBE_AND_DISCOVERY_OPERATIONS = frozenset(
    {
        ("get", "/.well-known/awf-core.json"),
        ("get", "/healthz"),
        ("get", "/readyz"),
    }
)

_API_TOKEN_PROTECTED_REST_OPERATIONS = frozenset(
    {
        ("get", "/release-readiness"),
        ("get", "/v1/events"),
        ("get", "/v1/locks"),
        ("get", "/v1/locks/overlap-graph"),
        ("get", "/v1/merge-queue"),
        ("get", "/v1/metrics/failures/summary"),
        ("get", "/v1/metrics/resources/saturation"),
        ("get", "/v1/metrics/slo"),
        ("get", "/v1/metrics/workspaces/summary"),
        ("get", "/v1/operations"),
        ("get", "/v1/operations/{operation_id}"),
        ("get", "/v1/tasks"),
        ("get", "/v1/tasks/{task_ref}/attempts"),
        ("get", "/v1/workspaces"),
        ("post", "/v1/workspaces"),
        ("post", "/v1/workspaces/adopt-pr"),
        ("get", "/v1/workspaces/overview"),
        ("delete", "/v1/workspaces/{workspace_id}"),
        ("get", "/v1/workspaces/{workspace_id}"),
        ("get", "/v1/workspaces/{workspace_id}/artifacts"),
        ("get", "/v1/workspaces/{workspace_id}/artifacts/download"),
        ("post", "/v1/workspaces/{workspace_id}/cancel"),
        ("get", "/v1/workspaces/{workspace_id}/events"),
        ("get", "/v1/workspaces/{workspace_id}/logs"),
        ("get", "/v1/workspaces/{workspace_id}/logs/{stream_id}"),
        ("get", "/v1/workspaces/{workspace_id}/operations"),
        ("post", "/v1/workspaces/{workspace_id}/rebase"),
        ("post", "/v1/workspaces/{workspace_id}/refresh"),
        ("post", "/v1/workspaces/{workspace_id}/remonitor"),
        ("post", "/v1/workspaces/{workspace_id}/retry"),
        ("get", "/v1/workspaces/{workspace_id}/runtime"),
        ("get", "/v1/workspaces/{workspace_id}/secret-leases"),
        ("get", "/v1/workspaces/{workspace_id}/stale-reasons"),
        ("post", "/v1/workspaces/{workspace_id}/stop"),
        ("post", "/v1/workspaces/{workspace_id}/validate"),
        ("get", "/v1/workspaces/{workspace_id}/validation"),
    }
)


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
        "/.well-known/awf-core.json",
        "/healthz",
        "/readyz",
        "/release-readiness",
        "/v1/workspaces",
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
def test_workspace_create_schema_components_use_canonical_v1_names(openapi_spec: dict) -> None:
    schemas = openapi_spec["components"]["schemas"]

    assert "WorkspaceV2" not in json.dumps(schemas, sort_keys=True)
    assert {
        "WorkspaceRepo",
        "WorkspaceTask",
        "WorkspaceProfileSelection",
        "WorkspaceValidation",
        "WorkspaceResources",
        "WorkspaceCompanionRequest",
    }.issubset(schemas)
    assert schemas["WorkspaceCreateRequest"]["properties"]["repo"]["$ref"] == (
        "#/components/schemas/WorkspaceRepo"
    )
    assert schemas["WorkspaceCreateRequest"]["properties"]["task"]["$ref"] == (
        "#/components/schemas/WorkspaceTask"
    )
    companion_schema = schemas["WorkspaceCreateRequest"]["properties"]["companions"]
    assert companion_schema["maxItems"] == 16
    assert companion_schema["items"]["$ref"] == "#/components/schemas/WorkspaceCompanionRequest"


@pytest.mark.unit
def test_workspace_companion_ports_document_container_first_order(openapi_spec: dict) -> None:
    description = openapi_spec["components"]["schemas"]["WorkspaceCompanionRequest"]["properties"][
        "ports"
    ]["description"]

    assert "[container_port, host_port]" in description
    assert "host_port:container_port" in description


@pytest.mark.unit
def test_workspace_companion_environment_keys_document_docker_names(
    openapi_spec: dict,
) -> None:
    environment = openapi_spec["components"]["schemas"]["WorkspaceCompanionRequest"]["properties"][
        "environment"
    ]

    assert environment["propertyNames"]["pattern"] == "^[A-Za-z_][A-Za-z0-9_]*$"


@pytest.mark.unit
def test_workspace_companion_environment_secret_keys_document_docker_names(
    openapi_spec: dict,
) -> None:
    environment_secrets = openapi_spec["components"]["schemas"]["WorkspaceCompanionRequest"][
        "properties"
    ]["environment_secrets"]

    assert environment_secrets["propertyNames"]["pattern"] == "^[A-Za-z_][A-Za-z0-9_]*$"


@pytest.mark.unit
def test_workspace_companion_compose_timeout_documents_bounds(openapi_spec: dict) -> None:
    timeout_schema = openapi_spec["components"]["schemas"]["WorkspaceCompanionRequest"][
        "properties"
    ]["compose_up_timeout_seconds"]
    integer_schema = next(item for item in timeout_schema["anyOf"] if item.get("type") == "integer")

    assert integer_schema["minimum"] == 1
    assert integer_schema["maximum"] == 1800


@pytest.mark.unit
def test_workspace_validation_commands_are_non_empty_in_openapi(openapi_spec: dict) -> None:
    command_items = openapi_spec["components"]["schemas"]["WorkspaceValidation"]["properties"][
        "commands"
    ]["items"]

    assert command_items["type"] == "string"
    assert command_items["minLength"] == 1


@pytest.mark.unit
def test_key_endpoint_methods_exist(openapi_spec: dict) -> None:
    paths = openapi_spec.get("paths", {})
    expected_methods: list[tuple[str, str]] = [
        ("POST", "/v1/workspaces"),
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
        ("GET", "/.well-known/awf-core.json"),
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
def test_callback_endpoints_advertise_bearer_auth_in_openapi(openapi_spec: dict) -> None:
    path = openapi_spec["paths"]["/v1/callbacks"]
    for method in ("get", "post"):
        operation = path[method]
        assert operation.get("security") == [{"bearerAuth": []}]
        authorization_params = [
            param
            for param in operation.get("parameters", [])
            if param.get("in") == "header" and str(param.get("name", "")).lower() == "authorization"
        ]
        assert authorization_params == []


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
            assert schema == {"$ref": _CALLBACK_HTTP_EXCEPTION_ERROR_RESPONSE_REF}

    post_422 = path["post"]["responses"]["422"]
    assert post_422["description"] == "Validation Error or Callback Target Policy Violation"
    assert post_422["content"]["application/json"]["schema"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/HTTPValidationError"},
            {"$ref": _CALLBACK_HTTP_EXCEPTION_ERROR_RESPONSE_REF},
        ],
        "title": "CallbackRegistrationUnprocessableEntityResponse",
    }

    wrapper_schema = openapi_spec["components"]["schemas"]["HTTPExceptionErrorResponse"]
    assert wrapper_schema["required"] == ["detail"]
    assert wrapper_schema["properties"]["detail"] == {
        "$ref": "#/components/schemas/ErrorResponse",
    }


@pytest.mark.unit
def test_openapi_does_not_model_bearer_auth_as_header_parameters(openapi_spec: dict) -> None:
    modeled_auth_headers: list[str] = []
    for path, path_item in openapi_spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if not isinstance(parameter, dict):
                    continue
                if (
                    parameter.get("in") == "header"
                    and str(parameter.get("name", "")).lower() == "authorization"
                ):
                    modeled_auth_headers.append(f"{method.upper()} {path}")

    assert modeled_auth_headers == []


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
def test_capacity_queue_planned_resources_uses_queue_specific_schema(
    openapi_spec: dict,
) -> None:
    schemas = openapi_spec["components"]["schemas"]
    planned_resources_ref = schemas["CapacityQueueSummaryResponse"]["properties"][
        "planned_resources"
    ]

    assert planned_resources_ref == {"$ref": "#/components/schemas/QueuePlannedResourcesResponse"}
    planned_resources = schemas["QueuePlannedResourcesResponse"]
    assert "active_workspace_count" not in planned_resources["properties"]
    assert set(planned_resources["required"]) == {
        "steady_cpu",
        "steady_memory_gb",
        "peak_cpu",
        "peak_memory_gb",
        "disk_mb",
        "dind_slots",
    }
    assert "active_workspace_count" in schemas["ReservedResourcesResponse"]["properties"]


@pytest.mark.unit
def test_capacity_queue_blocked_reason_counts_describes_fifo_frontiers(
    openapi_spec: dict,
) -> None:
    schemas = openapi_spec["components"]["schemas"]
    blocked_reason_counts = schemas["CapacityQueueSummaryResponse"]["properties"][
        "blocked_reason_counts"
    ]
    capacity_queue = schemas["ResourceSaturationSummaryResponse"]["properties"]["capacity_queue"]

    assert "FIFO frontier" in blocked_reason_counts["description"]
    assert "not every blocked workspace" in blocked_reason_counts["description"]
    assert "blocked_reason_counts counts the first FIFO frontier" in capacity_queue["description"]


@pytest.mark.unit
def test_spec_round_trips_to_json_and_back(openapi_spec: dict) -> None:
    serialized = json.dumps(openapi_spec, sort_keys=True)
    deserialized = json.loads(serialized)
    assert deserialized == openapi_spec, "Spec changed during JSON round-trip"


@pytest.mark.unit
def test_api_token_routes_are_documented_as_bearer_authenticated(
    openapi_spec: dict,
) -> None:
    security_schemes = openapi_spec.get("components", {}).get("securitySchemes", {})
    assert security_schemes.get("bearerAuth") == {
        "scheme": "bearer",
        "type": "http",
    }

    paths = openapi_spec.get("paths", {})
    for method, path in sorted(_API_TOKEN_PROTECTED_REST_OPERATIONS):
        operation = paths[path][method]
        assert operation.get("security") == [{"bearerAuth": []}], (
            f"{method.upper()} {path} must advertise bearer auth"
        )
        auth_header_params = [
            parameter
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "header"
            and str(parameter.get("name", "")).lower() == "authorization"
        ]
        assert auth_header_params == [], (
            f"{method.upper()} {path} must not model auth as an optional header parameter"
        )
        for status_code, description in (
            ("401", "Unauthorized"),
            ("503", "Service Unavailable"),
        ):
            response = operation.get("responses", {}).get(status_code)
            assert response is not None, f"{method.upper()} {path} must document {status_code}"
            assert response["description"] == description
            schema = response["content"]["application/json"]["schema"]
            refs = _schema_refs(schema)
            assert _HTTP_EXCEPTION_ERROR_RESPONSE_REF in refs
            if status_code == "401":
                assert refs == {_HTTP_EXCEPTION_ERROR_RESPONSE_REF}
                assert (
                    response.get("headers", {}).get("WWW-Authenticate") == _WWW_AUTHENTICATE_HEADER
                )
            if status_code == "503" and _ERROR_RESPONSE_REF in refs:
                assert refs == {_HTTP_EXCEPTION_ERROR_RESPONSE_REF, _ERROR_RESPONSE_REF}

    schemas = openapi_spec.get("components", {}).get("schemas", {})
    auth_error_schema = schemas.get("HttpExceptionErrorResponse", {})
    assert auth_error_schema.get("required") == ["detail"]
    assert (
        auth_error_schema.get("properties", {}).get("detail", {}).get("$ref") == _ERROR_RESPONSE_REF
    )


@pytest.mark.unit
def test_public_probe_and_discovery_routes_do_not_advertise_bearer_auth(
    openapi_spec: dict,
) -> None:
    paths = openapi_spec.get("paths", {})

    for method, path in sorted(_PUBLIC_PROBE_AND_DISCOVERY_OPERATIONS):
        operation = paths[path][method]
        assert operation.get("security") in (None, []), (
            f"{method.upper()} {path} must remain public for service probes/discovery"
        )
        assert "401" not in operation.get("responses", {})
        assert "503" not in operation.get("responses", {})


@pytest.mark.unit
def test_release_readiness_503_documents_failed_scorecard_body(openapi_spec: dict) -> None:
    operation = openapi_spec["paths"]["/release-readiness"]["get"]
    response = operation["responses"]["503"]

    assert response["description"] == "Service Unavailable"
    refs = _schema_refs(response["content"]["application/json"]["schema"])
    assert refs == {
        _HTTP_EXCEPTION_ERROR_RESPONSE_REF,
        _RELEASE_READINESS_RESPONSE_REF,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/callbacks"),
        ("post", "/v1/workspaces"),
    ],
)
def test_rate_limited_posts_document_retry_after_header(
    openapi_spec: dict,
    method: str,
    path: str,
) -> None:
    response = openapi_spec["paths"][path][method]["responses"]["429"]

    assert response["description"] == "Too Many Requests"
    assert response["content"]["application/json"]["schema"]["$ref"] == _ERROR_RESPONSE_REF
    assert response.get("headers", {}).get("Retry-After") == _RETRY_AFTER_HEADER


def _schema_refs(schema: dict) -> set[str]:
    direct_ref = schema.get("$ref")
    if isinstance(direct_ref, str):
        return {direct_ref}
    return {item["$ref"] for item in schema.get("anyOf", []) if isinstance(item.get("$ref"), str)}


@pytest.mark.unit
async def test_api_token_runtime_failures_match_documented_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(use_lifespan=False)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            monkeypatch.setenv("AWF_API_TOKEN", "secret")
            get_settings.cache_clear()

            missing = await client.get("/v1/operations")
            assert missing.status_code == 401
            assert missing.headers["WWW-Authenticate"] == "Bearer"
            assert missing.json()["detail"]["error_code"] == "UNAUTHORIZED"

            wrong = await client.get(
                "/v1/operations",
                headers={"Authorization": "Bearer wrong"},
            )
            assert wrong.status_code == 401
            assert wrong.headers["WWW-Authenticate"] == "Bearer"
            assert wrong.json()["detail"]["error_code"] == "UNAUTHORIZED"

            monkeypatch.setenv("AWF_API_TOKEN", "")
            get_settings.cache_clear()

            unconfigured = await client.get("/v1/operations")
            assert unconfigured.status_code == 503
            assert unconfigured.json()["detail"]["error_code"] == "API_TOKEN_NOT_CONFIGURED"

            health = await client.get("/healthz")
            assert health.status_code == 200
    finally:
        get_settings.cache_clear()
