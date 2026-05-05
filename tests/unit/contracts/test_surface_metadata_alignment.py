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
    if capability.auth_required:
        assert "require_api_token" in route.dependencies


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
    if capability.requires_idempotency_key:
        assert "idempotency_key" in tool.required
    if capability.supports_if_match:
        assert "expected_version" in tool.properties
        assert "expected_version" not in tool.required


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
