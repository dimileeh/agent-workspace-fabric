"""Trip-wire that prevents the contract registry from drifting from the matrix.

If a parity-matrix row gains a new mutating REST endpoint, this test fails on
collection and the registry must be updated before the harness is allowed to
ship. Conversely, if the harness declares a capability the matrix doesn't, this
test points at the inconsistency.
"""

from __future__ import annotations

import pytest

from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    all_capabilities,
    assert_capability_matches_parity_matrix,
    parity_capabilities_with_status,
    parity_mutating_capabilities_with_status,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    sorted(CAPABILITIES_BY_NAME),
)
def test_registry_capability_aligns_with_parity_matrix(capability_name: str) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    assert_capability_matches_parity_matrix(capability)


@pytest.mark.unit
def test_every_mutating_capability_with_mcp_tool_is_registered() -> None:
    """Every parity-matrix row that should be in the harness has an entry.

    In-scope rows: ``MCP implemented`` or ``MCP partial`` capabilities whose
    REST surface includes a POST/DELETE. Out-of-scope rows: read-only,
    Out of scope, or already-tracked-as-missing/backlog rows.
    """
    in_scope = set(
        parity_capabilities_with_status({"MCP implemented", "MCP partial"})
    )
    registered_capabilities = {c.parity_capability for c in all_capabilities()}

    expected_must_be_registered = set(
        parity_mutating_capabilities_with_status({"MCP implemented", "MCP partial"})
    )

    missing = expected_must_be_registered - registered_capabilities
    assert not missing, (
        "Contract registry is missing mutating capabilities exposed by REST+MCP "
        f"per the parity matrix: {sorted(missing)}. "
        "Add the row to tests/unit/contracts/_capabilities.py before contract tests."
    )

    not_in_matrix = registered_capabilities - in_scope - {
        "Refresh workspace",
        "Rebase workspace",
    }
    assert not not_in_matrix, (
        "Contract registry references parity-matrix capabilities that are no "
        f"longer 'MCP implemented'/'MCP partial': {sorted(not_in_matrix)}."
    )


@pytest.mark.unit
def test_mcp_partial_rows_track_named_backlog_slice() -> None:
    """Every MCP-partial capability in the registry surfaces a backlog slice.

    The backlog slice keeps the gap visible until the sibling P1 If-Match-parity
    slice flips MCP control tools to ``MCP implemented``.
    """
    for capability in all_capabilities():
        if capability.parity_status == "MCP partial":
            assert capability.parity_backlog_slice.startswith("TODO§"), (
                f"{capability.name}: MCP partial capabilities must reference a "
                f"backlog slice; got {capability.parity_backlog_slice!r}"
            )


@pytest.mark.unit
def test_mcp_missing_rows_track_named_backlog_slice() -> None:
    for capability in all_capabilities():
        if capability.parity_status == "MCP missing/backlog":
            assert capability.parity_backlog_slice.startswith("TODO§"), (
                f"{capability.name}: MCP missing/backlog capabilities must "
                f"reference a backlog slice; got {capability.parity_backlog_slice!r}"
            )
            assert capability.mcp_tool is None, (
                f"{capability.name}: MCP missing/backlog cannot declare an "
                f"MCP tool name; got {capability.mcp_tool!r}"
            )


@pytest.mark.unit
def test_mcp_implemented_rows_declare_mcp_tool() -> None:
    for capability in all_capabilities():
        if capability.parity_status == "MCP implemented":
            assert capability.mcp_tool is not None and capability.mcp_tool.startswith("awf_"), (
                f"{capability.name}: MCP implemented rows must declare an MCP "
                f"tool; got {capability.mcp_tool!r}"
            )
