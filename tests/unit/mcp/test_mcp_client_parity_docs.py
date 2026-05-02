from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"
README = REPO_ROOT / "README.md"

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


def _parse_markdown_table(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    in_table = False
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                in_table = False
            continue
        cells = [c.strip() for c in stripped.split("|")]
        cells = [c for c in cells if c != ""]
        if not headers:
            headers = cells
            in_table = True
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        row = {}
        for i, h in enumerate(headers):
            row[h] = cells[i] if i < len(cells) else ""
        rows.append(row)
    return rows


def _parity_rows() -> list[dict[str, str]]:
    doc = PARITY_DOC.read_text(encoding="utf-8")
    rows = _parse_markdown_table(doc)
    assert rows, "Parity matrix table should not be empty"
    return rows


def _strip_backticks(s: str) -> str:
    return re.sub(r"`+", "", s)


def _split_cell(cell: str) -> list[str]:
    cell = _strip_backticks(cell)
    parts = [p.strip() for p in cell.split(",")]
    return [p for p in parts if p]


@pytest.mark.unit
def test_mcp_client_parity_doc_publishes_roles_and_backlog_surfaces() -> None:
    doc = PARITY_DOC.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "docs/MCP_CLIENT_PARITY.md" in readme
    assert "REST is the canonical AWF control-plane API" in doc
    assert "CLI is a JSON-first operator convenience layer" in doc
    assert "MCP is a first-class parity client for agent orchestrators" in doc

    for missing_surface in (
        "awf_refresh_workspace",
        "awf_rebase_workspace",
        "awf_retry_workspace",
        "Artifact content/download",
        "If-Match",
    ):
        assert missing_surface in doc

    assert "not arbitrary shell" in doc
    assert "unrestricted Docker exec" in doc


@pytest.mark.unit
def test_parity_matrix_has_required_columns() -> None:
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
    rows = _parity_rows()
    for row in rows:
        status = row.get("Status", "").strip()
        assert status in STATUS_VOCABULARY, (
            f"Row '{row.get('Capability', '?')}' has status '{status}' "
            f"not in vocabulary {sorted(STATUS_VOCABULARY)}"
        )


@pytest.mark.unit
def test_implemented_rows_have_rest_endpoint_and_mcp_tool() -> None:
    rows = _parity_rows()
    for row in rows:
        status = row.get("Status", "").strip()
        if status == "MCP implemented":
            rest = row.get("Canonical REST surface", "").strip()
            mcp = row.get("MCP tool name", "").strip()
            assert rest, (
                f"Row '{row.get('Capability', '?')}' is MCP implemented "
                f"but has empty REST surface"
            )
            mcp_tools = _split_cell(mcp)
            assert any(t.startswith("awf_") for t in mcp_tools), (
                f"Row '{row.get('Capability', '?')}' is MCP implemented "
                f"but no MCP tool name starts with awf_ in '{mcp}'"
            )


@pytest.mark.unit
def test_partial_or_missing_rows_have_backlog_slice() -> None:
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
    rows = _parity_rows()
    for row in rows:
        boundary = row.get("Security Boundary", "").strip()
        assert boundary, (
            f"Row '{row.get('Capability', '?')}' has empty Security Boundary"
        )


@pytest.mark.unit
def test_schema_contract_column_non_empty_for_control_rows() -> None:
    rows = _parity_rows()
    for row in rows:
        rest = row.get("Canonical REST surface", "").strip()
        if "POST" in rest or "DELETE" in rest:
            schema_contract = row.get("Schema / Error-Code Contract", "").strip()
            assert schema_contract, (
                f"Row '{row.get('Capability', '?')}' has REST with POST/DELETE "
                f"but empty Schema / Error-Code Contract"
            )
