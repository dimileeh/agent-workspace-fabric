"""Unit tests for MCP client parity documentation constraints."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.mcp._parity_utils import (
    _parity_rows,
    _strip_backticks,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"
MCP_REFERENCE = REPO_ROOT / "docs" / "MCP_REFERENCE.md"
MCP_SETUP = REPO_ROOT / "docs" / "MCP_SETUP.md"

REQUIRED_COLUMNS = [
    "Capability",
    "Canonical REST surface",
    "CLI surface",
    "MCP tool name",
    "Schema / Error-Code Contract",
    "Security Boundary",
    "Status",
    "Backlog Slice",
]

STATUS_VOCABULARY = {
    "MCP implemented",
    "MCP partial",
    "MCP missing/backlog",
    "CLI absent",
    "Out of scope",
}

READ_ONLY_OPERATOR_ROWS = {
    "Workspace overview": {"awf_list_workspace_overview"},
    "Merge queue": {"awf_list_merge_queue"},
    "Task attempts": {"awf_list_tasks", "awf_list_task_attempts"},
    "Validation provenance": {"awf_list_workspace_validation"},
    "Stale reasons": {"awf_list_workspace_stale_reasons"},
    "Artifact metadata": {"awf_list_workspace_artifacts"},
    "Failure analysis metrics": {"awf_get_failure_analysis_summary"},
    "Workspace reliability metrics": {"awf_get_workspace_reliability_summary"},
    "Resource saturation metrics": {"awf_get_resource_saturation_summary"},
    "SLO metrics": {"awf_get_slo_metrics_summary"},
    "Locks and owned-path reservations": {"awf_list_locks"},
    "Advisory overlap graph": {"awf_get_overlap_graph"},
    "Service health and readiness": {
        "awf_get_service_health",
        "awf_get_service_readiness",
    },
    "Workspace events": {"awf_list_events", "awf_list_workspace_events"},
}

IDEMPOTENT_MCP_CONTROL_TOOLS = {
    "awf_cancel_workspace",
    "awf_stop_workspace",
    "awf_destroy_workspace",
    "awf_remonitor_workspace",
    "awf_request_workspace_validation",
    "awf_refresh_workspace",
    "awf_rebase_workspace",
}

FIRST_RUN_SETUP_TOOLS = {
    "awf_get_setup_status",
    "awf_start_local_service",
    "awf_initialize_project_profile",
    "awf_get_client_integration_instructions",
}


def _split_cell(cell: str) -> list[str]:
    """Split a cell by comma or semicolon, stripping whitespace and backticks."""
    cell = _strip_backticks(cell)
    # Support both comma and semicolon separation
    cell = cell.replace(";", ",")
    parts = [p.strip() for p in cell.split(",")]
    return [p for p in parts if p]


def _row_for_capability(rows: list[dict[str, str]], capability: str) -> dict[str, str]:
    """Return the row matching the given capability."""
    for row in rows:
        if row.get("Capability", "").strip() == capability:
            return row
    raise AssertionError(f"Missing parity matrix row for {capability!r}")


@pytest.mark.unit
def test_mcp_client_parity_doc_publishes_roles_and_backlog_surfaces() -> None:
    """Test mcp client parity doc publishes roles and backlog surfaces."""
    doc = PARITY_DOC.read_text(encoding="utf-8")
    mcp_ref = MCP_REFERENCE.read_text(encoding="utf-8")

    assert "MCP_CLIENT_PARITY.md" in mcp_ref
    assert "REST is the canonical AWF control-plane API" in doc
    assert "CLI is a JSON-first operator convenience layer" in doc
    assert "MCP is a first-class parity client for agent orchestrators" in doc
    for missing_surface in (
        "awf_refresh_workspace",
        "awf_rebase_workspace",
        "awf_retry_workspace",
        "awf_get_setup_status",
        "awf_start_local_service",
        "awf_initialize_project_profile",
        "awf_get_client_integration_instructions",
        "Artifact content/download",
        "If-Match",
    ):
        assert missing_surface in doc

    assert "not arbitrary shell" in doc
    assert "unrestricted Docker exec" in doc
    assert "no env-file contents" in doc


@pytest.mark.unit
def test_mcp_setup_publishes_claude_and_codex_stdio_config() -> None:
    """Test mcp setup publishes claude and codex stdio config."""
    setup_doc = MCP_SETUP.read_text(encoding="utf-8")
    mcp_ref = MCP_REFERENCE.read_text(encoding="utf-8")

    assert "claude mcp add --transport stdio" in setup_doc
    assert "awf mcp serve --env-file" in setup_doc
    assert "[mcp_servers.awf]" in setup_doc
    assert 'command = "awf"' in setup_doc
    assert "MCP_SETUP.md" in mcp_ref


@pytest.mark.unit
def test_mcp_and_cli_docs_do_not_claim_effort_is_excluded() -> None:
    """Test mcp and cli docs do not claim effort is excluded."""
    combined = "\n".join(
        [
            PARITY_DOC.read_text(encoding="utf-8"),
            MCP_REFERENCE.read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "CLI_REFERENCE.md").read_text(encoding="utf-8"),
        ]
    )

    assert "effort" in combined
    assert "effort` field is intentionally excluded" not in combined
    assert "is not exposed as a direct input flag" not in combined
    assert "awf_create_workspace" in combined

    import re

    assert not re.search(
        r"effort\s+(applies|scope|includes)\s+(to\s+)?(PR\s+)?(monitor\s+)?adoption",
        combined,
        flags=re.IGNORECASE,
    )
    assert "applies to pr adoption" not in combined.lower()
    assert "effort applies to pr" not in combined.lower()


@pytest.mark.unit
def test_parity_doc_no_longer_claims_read_tool_slice_is_backlog_only() -> None:
    """Test parity doc no longer claims read tool slice is backlog only."""
    doc = PARITY_DOC.read_text(encoding="utf-8")

    assert "this slice does not add MCP tools" not in " ".join(doc.split())


@pytest.mark.unit
def test_read_only_operator_rows_are_implementation_backed() -> None:
    """Test read only operator rows are implementation backed."""
    rows = _parity_rows()

    for capability, expected_tools in READ_ONLY_OPERATOR_ROWS.items():
        row = _row_for_capability(rows, capability)
        tools = set(_split_cell(row.get("MCP tool name", "")))
        backlog = _strip_backticks(row.get("Backlog Slice", "")).strip()

        assert row.get("Status", "").strip() == "MCP implemented", capability
        assert expected_tools <= tools, capability
        assert not any(tool.startswith("No ") for tool in tools), capability
        assert not backlog.startswith("TODO§"), capability


CONTROL_IMPLEMENTED_ROWS = {
    "Refresh workspace": ("awf workspace refresh", "awf_refresh_workspace"),
    "Rebase workspace": ("awf workspace rebase", "awf_rebase_workspace"),
}


@pytest.mark.unit
def test_control_operations_with_mcp_tools_are_documented() -> None:
    """Test control operations with mcp tools are documented."""
    rows = _parity_rows()

    for capability, (cli_surface, mcp_tool) in CONTROL_IMPLEMENTED_ROWS.items():
        row = _row_for_capability(rows, capability)
        cli_cell = _strip_backticks(row.get("CLI surface", "")).strip()
        mcp_cell = _strip_backticks(row.get("MCP tool name", "")).strip()
        backlog_cell = _strip_backticks(row.get("Backlog Slice", "")).strip()

        assert cli_surface in cli_cell, capability
        assert row.get("Status", "").strip() == "MCP implemented", capability
        assert mcp_tool in mcp_cell, capability
        assert not backlog_cell.startswith("TODO§"), capability


@pytest.mark.unit
def test_global_operations_safe_read_has_cli_parity() -> None:
    """Test global operations safe read has cli parity."""
    rows = _parity_rows()
    row = _row_for_capability(rows, "Global operations")
    cli_cell = _strip_backticks(row.get("CLI surface", "")).strip()
    backlog_cell = _strip_backticks(row.get("Backlog Slice", "")).strip()

    assert "CLI absent" not in cli_cell
    assert "awf operations list" in cli_cell
    assert "awf operations show" in cli_cell
    assert row.get("Status", "").strip() == "MCP implemented"
    assert backlog_cell == "—"


@pytest.mark.unit
def test_operation_read_rows_have_auth_parity_closed() -> None:
    """Test operation read rows have auth parity closed."""
    rows = _parity_rows()

    for capability in ("Workspace operations", "Global operations"):
        row = _row_for_capability(rows, capability)
        security_cell = row.get("Security Boundary", "")

        assert "require_api_token" in security_cell, capability
        assert "auth parity open" not in security_cell, capability
        assert row.get("Status", "").strip() == "MCP implemented", capability
        assert _strip_backticks(row.get("Backlog Slice", "")).strip() == "—"


@pytest.mark.unit
def test_retry_workspace_row_reflects_registered_mcp_tool() -> None:
    """Test retry workspace row reflects registered mcp tool."""
    rows = _parity_rows()
    row = _row_for_capability(rows, "Retry workspace")
    tools = set(_split_cell(row.get("MCP tool name", "")))
    backlog = _strip_backticks(row.get("Backlog Slice", "")).strip()

    assert "awf_retry_workspace" in tools
    assert not any(tool.startswith("No ") for tool in tools)
    assert row.get("Status", "").strip() in {"MCP implemented", "MCP partial"}
    assert backlog != "TODO§P1-mcp-retry"


@pytest.mark.unit
def test_first_run_setup_tools_are_documented_as_local_secret_free_mcp_surface() -> None:
    """Test first-run setup MCP tools are documented with their local security boundary."""
    rows = _parity_rows()
    row = _row_for_capability(rows, "Local first-run setup/start/init/client")
    tools = set(_split_cell(row.get("MCP tool name", "")))
    cli_cell = _strip_backticks(row.get("CLI surface", ""))
    rest_cell = _strip_backticks(row.get("Canonical REST surface", ""))
    security_cell = row.get("Security Boundary", "")
    parity_doc = PARITY_DOC.read_text(encoding="utf-8")
    mcp_ref = MCP_REFERENCE.read_text(encoding="utf-8")

    assert tools >= FIRST_RUN_SETUP_TOOLS
    assert row.get("Status", "").strip() == "MCP implemented"
    assert rest_cell == "Local first-run setup contract (REST unchanged)"
    for command in ("awf setup --dry-run", "awf start", "awf init", "awf setup --client"):
        assert command in cli_cell
    assert "no credential-value inputs" in security_cell
    assert "no env-file contents" in security_cell
    assert "safe refs/status only" in security_cell
    assert "source_checkout.marker_count" in parity_doc
    assert "integer | null" in parity_doc
    assert "explicit zero-client selections omit it" in " ".join(parity_doc.split())
    for tool_name in FIRST_RUN_SETUP_TOOLS:
        assert tool_name in mcp_ref


@pytest.mark.unit
def test_mcp_control_idempotency_migration_note_is_published() -> None:
    """Test mcp control idempotency migration note is published."""
    parity_doc = PARITY_DOC.read_text(encoding="utf-8")
    mcp_ref = MCP_REFERENCE.read_text(encoding="utf-8")
    combined = f"{parity_doc}\n{mcp_ref}"

    assert "MCP control migration note" in parity_doc
    assert "MCP control migration note" in mcp_ref
    assert "required `idempotency_key` argument" in combined
    assert "existing mcp clients" in combined.lower()
    assert "REST `Idempotency-Key`" in combined

    for tool_name in IDEMPOTENT_MCP_CONTROL_TOOLS:
        assert tool_name in parity_doc
        assert tool_name in mcp_ref


@pytest.mark.unit
def test_parity_matrix_has_required_columns() -> None:
    """Test parity matrix has required columns."""
    doc = PARITY_DOC.read_text(encoding="utf-8")
    lines = doc.splitlines()
    header_line: str | None = None
    for line in lines:
        if line.strip().startswith("|") and "Capability" in line:
            header_line = line.strip()
            break
    assert header_line is not None, "Could not find parity matrix header row"
    cells = [c.strip() for c in header_line.split("|") if c.strip()]
    for col in REQUIRED_COLUMNS:
        assert col in cells, f"Missing required column: {col}"


@pytest.mark.unit
def test_parity_matrix_status_values_are_from_vocabulary() -> None:
    """Test parity matrix status values are from vocabulary."""
    rows = _parity_rows()
    for row in rows:
        status = row.get("Status", "").strip()
        assert status in STATUS_VOCABULARY, (
            f"Row '{row.get('Capability', '?')}' has status '{status}' "
            f"not in vocabulary {sorted(STATUS_VOCABULARY)}"
        )


@pytest.mark.unit
def test_implemented_rows_have_rest_endpoint_and_mcp_tool() -> None:
    """Test implemented rows have rest endpoint and mcp tool."""
    rows = _parity_rows()
    for row in rows:
        status = row.get("Status", "").strip()
        if status == "MCP implemented":
            rest = row.get("Canonical REST surface", "").strip()
            mcp = row.get("MCP tool name", "").strip()
            assert rest, (
                f"Row '{row.get('Capability', '?')}' is MCP implemented but has empty REST surface"
            )
            mcp_tools = _split_cell(mcp)
            assert any(t.startswith("awf_") for t in mcp_tools), (
                f"Row '{row.get('Capability', '?')}' is MCP implemented "
                f"but no MCP tool name starts with awf_ in '{mcp}'"
            )


@pytest.mark.unit
def test_partial_or_missing_rows_have_backlog_slice() -> None:
    """Test partial or missing rows have backlog slice."""
    rows = _parity_rows()
    for row in rows:
        status = row.get("Status", "").strip()
        if "partial" in status or "missing" in status:
            backlog = row.get("Backlog Slice", "").strip()
            backlog = _strip_backticks(backlog)
            assert backlog.startswith("TODO§"), (
                f"Row '{row.get('Capability', '?')}' has status '{status}' "
                f"but Backlog Slice '{backlog}' doesn't start with TODO§"
            )


@pytest.mark.unit
def test_security_boundary_column_non_empty() -> None:
    """Test security boundary column non empty."""
    rows = _parity_rows()
    for row in rows:
        boundary = row.get("Security Boundary", "").strip()
        assert boundary, f"Row '{row.get('Capability', '?')}' has empty Security Boundary"


@pytest.mark.unit
def test_schema_contract_column_non_empty_for_control_rows() -> None:
    """Test schema contract column non empty for control rows."""
    rows = _parity_rows()
    for row in rows:
        rest = row.get("Canonical REST surface", "").strip()
        if "POST" in rest or "DELETE" in rest:
            schema_contract = row.get("Schema / Error-Code Contract", "").strip()
            assert schema_contract, (
                f"Row '{row.get('Capability', '?')}' has REST with POST/DELETE "
                f"but empty Schema / Error-Code Contract"
            )


@pytest.mark.unit
def test_parity_matrix_matches_real_surfaces() -> None:
    """Test parity matrix matches real surfaces."""
    from unittest.mock import MagicMock

    import typer

    from awf.api.app import create_app
    from awf.cli.main import app as cli_app
    from awf.common.config import Settings
    from awf.mcp.server import build_mcp_server

    # 1. Load real REST routes
    rest_app = create_app(use_lifespan=False)
    real_rest_routes = set()
    for route in rest_app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:
                real_rest_routes.add(f"{method} {route.path}")

    # 2. Load real CLI commands
    def get_cli_commands(app: typer.Typer, prefix: str = "awf") -> list[str]:
        """Extract all CLI commands from the Typer app recursively."""
        commands = []
        if app.registered_commands:
            for cmd in app.registered_commands:
                if cmd.name:
                    commands.append(f"{prefix} {cmd.name}".strip())
        if app.registered_groups:
            for grp in app.registered_groups:
                if grp.name and grp.typer_instance:
                    commands.extend(get_cli_commands(grp.typer_instance, f"{prefix} {grp.name}"))
        return commands

    real_cli_commands = set(get_cli_commands(cli_app))

    import asyncio

    # 3. Load real MCP tools
    mcp = build_mcp_server(service=MagicMock(), settings=Settings(_env_file=None))
    real_mcp_tools = {t.name for t in asyncio.run(mcp.list_tools())}

    # 4. Iterate over rows
    rows = _parity_rows()
    for row in rows:
        capability = row.get("Capability", "").strip()

        # Check REST
        rest_cell = row.get("Canonical REST surface", "").strip()
        rest_paths = _split_cell(rest_cell)
        for rest_path in rest_paths:
            if not rest_path:
                continue
            verb = rest_path.split()[0]
            if verb in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                assert rest_path in real_rest_routes, (
                    f"REST path {rest_path!r} for {capability!r} not in real FastAPI routes"
                )

        # Check CLI
        cli_cell = row.get("CLI surface", "").strip()
        if "CLI absent" not in cli_cell:
            cli_paths = _split_cell(cli_cell)
            if row.get("Status", "").strip() == "MCP implemented":
                assert cli_paths, (
                    f"CLI surface must be documented for {capability!r} (or explicitly marked 'CLI absent')"
                )
            for cli_path in cli_paths:
                if not cli_path:
                    continue
                # The real CLI command must be an exact match or a prefix followed by a space
                assert any(
                    cli_path == c or cli_path.startswith(c + " ") for c in real_cli_commands
                ), (
                    f"CLI usage {cli_path!r} for {capability!r} does not match any real Typer command prefix"
                )

        # Check MCP
        mcp_cell = row.get("MCP tool name", "").strip()
        mcp_tools = _split_cell(mcp_cell)
        for mcp_tool in mcp_tools:
            if not mcp_tool or "No MCP tool" in mcp_tool or mcp_tool == "No streaming MCP tool":
                continue
            if mcp_tool.startswith("No "):
                missing_tool = mcp_tool[3:].strip()
                if " " in missing_tool:
                    continue  # Plain English description, not a tool name
                assert missing_tool not in real_mcp_tools, (
                    f"MCP tool {missing_tool!r} for {capability!r} is marked 'No' but exists in real FastMCP tools"
                )
            else:
                assert mcp_tool in real_mcp_tools, (
                    f"MCP tool {mcp_tool!r} for {capability!r} not in real FastMCP tools"
                )
