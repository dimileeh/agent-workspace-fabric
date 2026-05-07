"""Registry-driven metadata checks for REST, CLI, and MCP surfaces."""

from __future__ import annotations

import pytest

from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    all_capabilities,
    cli_capabilities,
    control_capabilities,
    implemented_surface_capabilities,
    mcp_capabilities,
    parity_capability_cli_surface,
)
from tests.unit.contracts._introspection import cli_commands, mcp_tools, rest_routes

FORBIDDEN_MCP_TOOL_PREFIXES = (
    "awf_shell",
    "awf_exec",
    "awf_run_command",
    "awf_run_shell",
    "awf_docker_exec",
    "awf_container_exec",
    "awf_read_file",
    "awf_list_files",
    "awf_read_secret",
    "awf_list_secret",
    "awf_read_workspace_artifact",
    "awf_download_workspace_artifact",
)

FORBIDDEN_MCP_INPUTS = {
    "api_token",
    "artifact_path",
    "authorization",
    "bearer",
    "command",
    "container_id",
    "docker_command",
    "host_path",
    "path",
    "secret_name",
    "shell",
    "token",
}

CREATE_V2_FULL_PARITY_FIELDS = frozenset({"resources", "priority", "human_boost"})
CREATE_V2_FULL_PARITY_BACKLOG = "TODO§P1-mcp-create-v2-full-parity"
STATUS_FILTER_PARITY_CAPABILITIES = (
    "workspace_overview",
    "merge_queue",
    "locks",
)
OPERATION_TYPE_FILTER_PARITY_CAPABILITIES = (
    "workspace_operations",
    "global_operations",
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    sorted(CAPABILITIES_BY_NAME),
)
def test_rest_route_metadata_matches_registry(capability_name: str) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    routes = rest_routes()
    route = routes.get((capability.rest_method, capability.rest_path))
    assert route is not None, (
        f"{capability.name}: missing REST route "
        f"{capability.rest_method} {capability.rest_path}"
    )

    if capability.rest_response_model is not None:
        assert route.response_model == capability.rest_response_model
    assert route.path_fields == capability.rest_path_fields
    assert route.query_fields == capability.rest_query_fields
    assert route.header_fields == capability.rest_header_fields
    assert route.body_fields == capability.rest_body_fields
    has_auth_dependency = "require_api_token" in route.dependencies
    assert capability.auth_required is has_auth_dependency, (
        f"{capability.name}: registry auth_required={capability.auth_required} "
        f"but REST dependencies are {sorted(route.dependencies)}"
    )


@pytest.mark.unit
def test_rest_query_metadata_uses_public_aliases() -> None:
    routes = rest_routes()

    assert routes[("GET", "/v1/workspaces")].query_fields >= {
        "status",
        "agent",
        "repo_url",
        "limit",
    }
    assert "workspace_status" not in routes[("GET", "/v1/workspaces")].query_fields
    assert routes[("GET", "/v1/operations")].query_fields >= {
        "workspace_id",
        "status",
        "type",
        "limit",
    }
    assert "operation_type" not in routes[("GET", "/v1/operations")].query_fields


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", STATUS_FILTER_PARITY_CAPABILITIES)
def test_implemented_mcp_status_filter_names_match_rest_alias(
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]

    assert capability.is_mcp_implemented
    assert "status" in capability.rest_query_fields
    assert "status" in capability.mcp_request_fields
    assert "workspace_status" not in capability.mcp_request_fields


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", OPERATION_TYPE_FILTER_PARITY_CAPABILITIES)
def test_implemented_mcp_operation_type_filter_name_matches_rest_alias(
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]

    assert capability.mcp_tool is not None
    assert "type" in capability.rest_query_fields
    assert "type" in capability.mcp_request_fields
    assert "operation_type" not in capability.mcp_request_fields


@pytest.mark.unit
def test_cli_surface_presence_is_explicit_per_parity_row() -> None:
    """Rows with CLI commands must register one; CLI-absent rows must register none."""
    rows = {capability.parity_capability for capability in implemented_surface_capabilities()}
    for parity_capability in sorted(rows):
        registered = [
            capability
            for capability in implemented_surface_capabilities()
            if capability.parity_capability == parity_capability
        ]
        cli_surface = parity_capability_cli_surface(parity_capability)
        has_registered_cli = any(capability.cli_tokens is not None for capability in registered)
        if cli_surface == "CLI absent":
            assert not has_registered_cli, (
                f"{parity_capability}: matrix says CLI absent but registry declares "
                f"{[capability.cli_tokens for capability in registered if capability.cli_tokens]}"
            )
        elif cli_surface and cli_surface != "N/A":
            assert has_registered_cli, (
                f"{parity_capability}: matrix lists CLI surface {cli_surface!r} but "
                "no registry row declares executable CLI coverage."
            )


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    sorted(capability.name for capability in cli_capabilities()),
)
def test_cli_command_shape_matches_registry(capability_name: str) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    assert capability.cli_tokens is not None
    commands = cli_commands()
    command = commands.get(capability.cli_tokens)
    assert command is not None, f"{capability.name}: missing CLI command {capability.cli_tokens}"
    assert capability.cli_options <= command.options
    assert capability.cli_arguments <= command.arguments


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    sorted(capability.name for capability in mcp_capabilities()),
)
async def test_mcp_tool_schema_matches_registry(capability_name: str) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    assert capability.mcp_tool is not None
    tools = await mcp_tools()
    tool = tools.get(capability.mcp_tool)
    assert tool is not None, f"{capability.name}: missing MCP tool {capability.mcp_tool}"

    assert capability.mcp_request_fields <= tool.properties
    assert capability.mcp_required_fields <= tool.required
    if "idempotency_key" in capability.mcp_request_fields:
        assert "idempotency_key" in tool.properties
        if capability.requires_idempotency_key:
            assert "idempotency_key" in tool.required
        else:
            assert "idempotency_key" not in tool.required
    if capability.supports_if_match:
        assert "expected_version" in tool.properties
        assert "expected_version" not in tool.required


@pytest.mark.unit
async def test_create_v2_registry_status_tracks_mcp_payload_parity_gap() -> None:
    capability = CAPABILITIES_BY_NAME["create_workspace_v2"]
    tool = (await mcp_tools())["awf_create_workspace_v2"]
    missing = CREATE_V2_FULL_PARITY_FIELDS - tool.properties

    expected_status = "MCP partial" if missing else "MCP implemented"
    expected_backlog = CREATE_V2_FULL_PARITY_BACKLOG if missing else "—"

    assert capability.parity_status == expected_status
    assert capability.parity_backlog_slice == expected_backlog


@pytest.mark.unit
async def test_mcp_surface_stays_inside_operator_safety_boundary() -> None:
    tools = await mcp_tools()
    for tool_name, tool in tools.items():
        forbidden_prefix = next(
            (prefix for prefix in FORBIDDEN_MCP_TOOL_PREFIXES if tool_name.startswith(prefix)),
            None,
        )
        assert forbidden_prefix is None, f"{tool_name}: forbidden unsafe MCP tool prefix"
        forbidden_inputs = FORBIDDEN_MCP_INPUTS & set(tool.properties)
        assert not forbidden_inputs, f"{tool_name}: forbidden unsafe inputs {sorted(forbidden_inputs)}"


@pytest.mark.unit
def test_idempotency_key_requirement_is_distinct_from_optional_support() -> None:
    required_capabilities = {
        capability.name
        for capability in all_capabilities()
        if capability.requires_idempotency_key
    }
    supported_capabilities = {
        capability.name
        for capability in all_capabilities()
        if capability.supports_idempotency_key
    }

    assert required_capabilities == {capability.name for capability in control_capabilities()}
    assert required_capabilities <= supported_capabilities
    assert CAPABILITIES_BY_NAME["create_workspace_v1"].supports_idempotency_key is True
    assert CAPABILITIES_BY_NAME["create_workspace_v1"].requires_idempotency_key is False
    assert CAPABILITIES_BY_NAME["create_workspace_v2"].supports_idempotency_key is True
    assert CAPABILITIES_BY_NAME["create_workspace_v2"].requires_idempotency_key is False


@pytest.mark.unit
def test_mcp_idempotency_support_distinguishes_optional_and_required_keys() -> None:
    for capability in mcp_capabilities():
        if not capability.supports_idempotency_key:
            continue

        assert "Idempotency-Key" in capability.rest_header_fields
        assert "idempotency_key" in capability.mcp_request_fields
        if capability.requires_idempotency_key:
            assert "idempotency_key" in capability.mcp_required_fields
        else:
            assert "idempotency_key" not in capability.mcp_required_fields


@pytest.mark.unit
def test_versioned_control_registry_declares_client_semantics() -> None:
    """Cross-cutting If-Match matrix row is represented by all versioned controls."""
    expected = {
        "cancel_workspace",
        "stop_workspace",
        "destroy_workspace",
        "remonitor_workspace",
        "request_validation",
        "refresh_workspace",
        "rebase_workspace",
    }
    actual = {capability.name for capability in control_capabilities()}
    assert actual == expected
    for capability in control_capabilities():
        assert capability.supports_idempotency_key is True
        assert capability.supports_if_match is True
        assert "Idempotency-Key" in capability.rest_header_fields
        assert "If-Match" in capability.rest_header_fields
        assert "idempotency_key" in capability.mcp_request_fields
        assert "expected_version" in capability.mcp_request_fields
        assert capability.parity_status == "MCP implemented"


@pytest.mark.unit
def test_missing_or_partial_surfaces_have_backlog_status() -> None:
    for capability in all_capabilities():
        if capability.parity_status in {"MCP partial", "MCP missing/backlog"}:
            assert capability.parity_backlog_slice.startswith("TODO§"), capability
        if capability.parity_status == "MCP missing/backlog":
            assert capability.mcp_tool is None, capability
