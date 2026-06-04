"""Trip-wire that prevents the contract registry from drifting from the matrix.

If a parity-matrix row gains a new mutating REST endpoint, this test fails on
collection and the registry must be updated before the harness is allowed to
ship. Conversely, if the harness declares a capability the matrix doesn't, this
test points at the inconsistency.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    all_capabilities,
    assert_capability_matches_parity_matrix,
    parity_capabilities_with_status,
    parity_endpoint_capabilities_with_status,
)
from tests.unit.mcp._parity_utils import (
    IMPLEMENTED_STATUS,
    MISSING_STATUS,
    TODO_DOC,
    _extract_mcp_tool_tokens_from_cell,
    _is_partial_or_missing_row,
    _parity_backlog_slice,
    _parity_rows,
    _parity_status,
    _unchecked_todo_markers,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

IMPLEMENTED_PARITY_COVERAGE_REFERENCES: dict[str, tuple[str, ...]] = {
    "Workspace create": (
        "tests/unit/contracts/test_request_payload_alignment.py::test_mcp_create_hydrates_canonical_request_model",
    ),
    "Workspace list and get": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py::TestGetAndList::test_get_returns_the_workspace_just_created",
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py::TestGetAndList::test_list_returns_newest_first",
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWaitForWorkspace::test_exits_immediately_when_already_terminal",
    ),
    "Workspace overview": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_004.py::TestMcpOperatorSurfaceParityPart003::test_workspace_overview_tool_matches_rest_payload",
    ),
    "Merge queue": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_004.py::TestMcpOperatorSurfaceParityPart003::test_merge_queue_tool_matches_rest_payload_and_reason_codes",
    ),
    "Task attempts": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_task_listing_tool_matches_rest_payload",
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_task_attempts_tool_matches_rest_payload",
    ),
    "Validation provenance": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_004.py::TestMcpOperatorSurfaceParityPart003::test_validation_provenance_tool_matches_rest_payload",
    ),
    "Stale reasons": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_004.py::TestMcpOperatorSurfaceParityPart003::test_stale_reasons_tool_matches_rest_active_and_resolved_payloads",
    ),
    "Artifact metadata": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_004.py::TestMcpOperatorSurfaceParityPart003::test_artifacts_tool_matches_rest_metadata_payload",
    ),
    "Failure analysis metrics": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_failure_analysis_metrics_tool_matches_rest_payload",
    ),
    "Workspace reliability metrics": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_workspace_reliability_and_slo_tools_match_rest_payloads",
    ),
    "Resource saturation metrics": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_resource_saturation_tool_matches_rest_payload_with_fake_providers",
    ),
    "SLO metrics": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_workspace_reliability_and_slo_tools_match_rest_payloads",
    ),
    "Locks and owned-path reservations": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_locks_tool_matches_rest_payload",
    ),
    "Advisory overlap graph": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_overlap_graph_tool_matches_rest_payload",
    ),
    "Service health and readiness": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_service_health_tool_returns_healthz_payload",
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_service_readiness_tool_matches_rest_payload",
    ),
    "Core release readiness scorecard": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py::TestMcpOperatorSurfaceParityPart001::test_core_release_readiness_tool_matches_rest_payload",
    ),
    "Workspace runtime snapshot": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceRuntime::test_get_workspace_runtime_returns_container_snapshot",
    ),
    "Durable workspace logs": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams",
    ),
    "Artifact content/download": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py::TestReadWorkspaceArtifact::test_reads_safe_small_file_and_returns_base64_content",
    ),
    "Workspace operations": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_operations_tool_matches_rest_filters_and_detail",
    ),
    "Global operations": (
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_operations_tool_matches_rest_filters_and_detail",
    ),
    "Cancel workspace": (
        "tests/unit/contracts/test_request_payload_alignment.py::test_mcp_cancel_invokes_service_with_canonical_kwargs",
    ),
    "Stop workspace stack": (
        "tests/unit/contracts/test_response_payload_alignment.py::test_stop_rest_matches_mcp_structured_content",
    ),
    "Destroy workspace resources": (
        "tests/unit/contracts/test_request_payload_alignment.py::test_mcp_destroy_invokes_service_with_canonical_kwargs",
    ),
    "Remonitor workspace": (
        "tests/unit/contracts/test_reason_code_alignment.py::test_rest_and_mcp_agree_on_remonitor_pr_required",
    ),
    "Request validation": (
        "tests/unit/contracts/test_reason_code_alignment.py::test_rest_and_mcp_agree_on_validate_state_not_validatable",
    ),
    "Refresh workspace": (
        "tests/unit/mcp/test_mcp_control_contracts.py::TestRealDbPaths::test_refresh_creates_operation_row",
    ),
    "Rebase workspace": (
        "tests/unit/mcp/test_mcp_control_contracts.py::TestRealDbPaths::test_rebase_creates_operation_row",
    ),
    "Retry workspace": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py::TestCreateWorkspace::test_retry_workspace_provider_preflight_error_and_override",
    ),
    "Existing PR monitor adoption": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_001.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_creates_adoption",
    ),
    "Workspace events": (
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestGlobalEvents::test_list_events_returns_empty_list",
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestGlobalEvents::test_list_events_returns_events_across_workspaces",
        "tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceEvents::test_lists_workspace_events_with_envelope_and_has_more",
        "tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py::TestMcpOperatorSurfaceParityPart001::test_empty_read_only_operator_surfaces_match_rest_payloads",
    ),
    "Optimistic concurrency on controls": (
        "tests/unit/contracts/test_if_match_alignment.py::test_mcp_control_tools_expose_optional_expected_version",
        "tests/unit/contracts/test_if_match_alignment.py::test_rest_stale_if_match_returns_version_conflict_envelope",
    ),
}


def _assert_test_reference_exists(reference: str) -> None:
    path_text, _, node_id = reference.partition("::")
    path = REPO_ROOT / path_text
    assert path.is_file(), f"Coverage reference {reference!r} points at a missing file"
    assert node_id, f"Coverage reference {reference!r} must include a pytest node ID"
    collected = _source_test_node_ids(str(REPO_ROOT), path_text)
    assert reference in collected, (
        f"Coverage reference {reference!r} is not a collected pytest node ID. "
        f"Collected {len(collected)} node IDs from {path_text!r}."
    )


@cache
def _source_test_node_ids(repo_root_text: str, path_text: str) -> frozenset[str]:
    source_path = Path(repo_root_text) / path_text
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=path_text)
    node_ids: set[str] = set()

    def collect_class(body: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for child in body:
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef):
                if child.name.startswith("test_"):
                    node_ids.add(f"{path_text}::{'::'.join((*parents, child.name))}")
                continue
            if isinstance(child, ast.ClassDef) and child.name.startswith("Test"):
                collect_class(child.body, (*parents, child.name))

    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if node.name.startswith("test_"):
                node_ids.add(f"{path_text}::{node.name}")
            continue
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            collect_class(node.body, (node.name,))

    return frozenset(node_ids)


@pytest.mark.unit
def test_assert_test_reference_exists_accepts_indented_test_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_nested.py"
    test_file.write_text(
        "\n".join(
            (
                "class TestOuter:",
                "    class TestInner:",
                "        def test_nested_reference(self) -> None:",
                "            pass",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("tests.unit.contracts.test_registry_smoke.REPO_ROOT", tmp_path)

    _assert_test_reference_exists("test_nested.py::TestOuter::TestInner::test_nested_reference")


@pytest.mark.unit
def test_assert_test_reference_exists_rejects_test_moved_to_another_class(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_moved.py"
    test_file.write_text(
        "\n".join(
            (
                "class TestExpected:",
                "    def test_other_reference(self) -> None:",
                "        pass",
                "",
                "class TestActual:",
                "    def test_target_reference(self) -> None:",
                "        pass",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("tests.unit.contracts.test_registry_smoke.REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError, match="is not a collected pytest node ID"):
        _assert_test_reference_exists("test_moved.py::TestExpected::test_target_reference")


@pytest.mark.unit
@pytest.mark.parametrize(
    "capability_name",
    sorted(CAPABILITIES_BY_NAME),
)
def test_registry_capability_aligns_with_parity_matrix(capability_name: str) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    assert_capability_matches_parity_matrix(capability)


@pytest.mark.unit
def test_every_safe_read_or_control_capability_with_mcp_surface_is_registered() -> None:
    """Every parity-matrix row that should be in the harness has an entry.

    In-scope rows: ``MCP implemented`` or ``MCP partial`` capabilities whose
    REST surface includes a safe read/control endpoint. Out-of-scope rows:
    explicit Out of scope or already-tracked-as-missing/backlog rows.
    """
    in_scope = set(parity_capabilities_with_status({"MCP implemented", "MCP partial"}))
    registered_capabilities = {c.parity_capability for c in all_capabilities()}

    expected_must_be_registered = set(
        parity_endpoint_capabilities_with_status({"MCP implemented", "MCP partial"})
    )

    missing = expected_must_be_registered - registered_capabilities
    assert not missing, (
        "Contract registry is missing safe read/control capabilities exposed by REST+MCP "
        f"per the parity matrix: {sorted(missing)}. "
        "Add the row to tests/unit/contracts/_capabilities.py before contract tests."
    )

    not_in_matrix = registered_capabilities - in_scope - {"Artifact content/download"}
    assert not not_in_matrix, (
        "Contract registry references parity-matrix capabilities that are no "
        f"longer 'MCP implemented'/'MCP partial': {sorted(not_in_matrix)}."
    )


@pytest.mark.unit
def test_mcp_implemented_matrix_rows_have_executable_coverage_reference() -> None:
    rows = _parity_rows()
    implemented_capabilities = {
        row["Capability"].strip()
        for row in rows
        if _parity_status(row) == IMPLEMENTED_STATUS
        and _extract_mcp_tool_tokens_from_cell(row.get("MCP tool name", ""))
    }
    stale_references = set(IMPLEMENTED_PARITY_COVERAGE_REFERENCES) - implemented_capabilities
    assert not stale_references, (
        "Coverage map references parity rows that are no longer MCP implemented: "
        f"{sorted(stale_references)}."
    )

    missing = sorted(
        capability
        for capability in implemented_capabilities
        if capability not in IMPLEMENTED_PARITY_COVERAGE_REFERENCES
    )
    assert not missing, (
        "MCP implemented parity rows lack executable contract/parity coverage: "
        f"{missing}. Add an explicit test reference."
    )

    for references in IMPLEMENTED_PARITY_COVERAGE_REFERENCES.values():
        for reference in references:
            _assert_test_reference_exists(reference)


@pytest.mark.unit
def test_non_implemented_matrix_rows_track_unchecked_backlog_slice() -> None:
    active_todo_markers = (
        _unchecked_todo_markers(TODO_DOC.read_text(encoding="utf-8"))
        if TODO_DOC.exists()
        else set()
    )
    failures: list[str] = []

    for row in _parity_rows():
        if not _is_partial_or_missing_row(row):
            continue
        capability = row.get("Capability", "?")
        status = _parity_status(row)
        backlog = _parity_backlog_slice(row)
        if not backlog.startswith("TODO§"):
            failures.append(f"{capability}: {status} row has no TODO§ backlog slice")
            continue
        if backlog not in active_todo_markers:
            failures.append(f"{capability}: {backlog} is not listed in an unchecked TODO item")
        if status == MISSING_STATUS and _extract_mcp_tool_tokens_from_cell(
            row.get("MCP tool name", "")
        ):
            failures.append(f"{capability}: missing/backlog row declares a live MCP tool")

    assert not failures, (
        "Partial or missing parity rows must keep active backlog visibility:\n"
        + "\n".join(failures)
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
