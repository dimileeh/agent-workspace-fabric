from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"
TODO_DOC = REPO_ROOT / "TODO" / "pre-gke-industrial-readiness.md"


IMPLEMENTED_STATUS = "MCP implemented"
PARTIAL_STATUS = "MCP partial"
MISSING_STATUS = "MCP missing/backlog"
OUT_OF_SCOPE_STATUS = "Out of scope"


@dataclass(frozen=True)
class CliInvocation:
    source: str
    command: str
    flags: tuple[str, ...]


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
                headers = []
            continue
        cells = [c.strip() for c in stripped.split("|")]
        if cells and cells[0] == "":
            cells.pop(0)
        if cells and cells[-1] == "":
            cells.pop()
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


def _normalize_cell(cell: str) -> str:
    return " ".join(_strip_backticks(cell).split()).strip()


def _split_matrix_cell(cell: str) -> list[str]:
    cleaned = _strip_backticks(cell).replace(";", ",")
    return [part.strip() for part in cleaned.split(",") if part.strip()]


def _parity_status(row: dict[str, str]) -> str:
    return _normalize_cell(row.get("Status", ""))


def _parity_backlog_slice(row: dict[str, str]) -> str:
    return _normalize_cell(row.get("Backlog Slice", ""))


def _is_implemented_row(row: dict[str, str]) -> bool:
    return _parity_status(row) == IMPLEMENTED_STATUS


def _is_partial_or_missing_row(row: dict[str, str]) -> bool:
    return _parity_status(row) in {PARTIAL_STATUS, MISSING_STATUS}


_REST_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH|WS|WebSocket)\s+(/[^\s,;`]+)")


def _extract_rest_endpoints_from_cell(cell: str) -> list[tuple[str, str]]:
    cleaned = _strip_backticks(cell)
    endpoints: list[tuple[str, str]] = []
    for match in _REST_ENDPOINT_RE.finditer(cleaned):
        method = match.group(1).upper()
        if method == "WEBSOCKET":
            method = "WS"
        endpoints.append((method, match.group(2).rstrip(",")))
    return endpoints


def _rest_cell_names_endpoints(cell: str) -> bool:
    return bool(_REST_ENDPOINT_RE.search(_strip_backticks(cell)))


def _extract_mcp_tool_tokens_from_cell(cell: str) -> list[str]:
    tools: list[str] = []
    for part in _split_matrix_cell(cell):
        if part.lower().startswith("no "):
            continue
        tools.extend(re.findall(r"\bawf_[A-Za-z0-9_]+\b", part))
    return tools


def _extract_cli_invocations_from_cell(cell: str) -> list[CliInvocation]:
    cleaned = _normalize_cell(cell)
    if not cleaned or cleaned == "CLI absent":
        return []

    invocations: list[CliInvocation] = []
    for part in _split_matrix_cell(cell):
        if not part:
            continue
        tokens = part.split()
        base_end = len(tokens)
        for index, token in enumerate(tokens):
            if token.startswith("--") or token.startswith("<"):
                base_end = index
                break
        command = " ".join(tokens[:base_end])
        flags = tuple(
            token.split("=", 1)[0] for token in tokens[base_end:] if token.startswith("--")
        )
        invocations.append(CliInvocation(source=part, command=command, flags=flags))
    return invocations


def _unchecked_todo_markers(todo_text: str) -> set[str]:
    markers: set[str] = set()
    current_unchecked_block: list[str] = []

    def flush_current_block() -> None:
        if current_unchecked_block:
            block = "\n".join(current_unchecked_block)
            markers.update(re.findall(r"TODO§[A-Za-z0-9_.-]+", block))

    for line in todo_text.splitlines():
        if re.match(r"^\s*-\s+\[[ xX]\]\s+", line):
            flush_current_block()
            current_unchecked_block = [line] if re.match(r"^\s*-\s+\[\s\]\s+", line) else []
            continue
        if current_unchecked_block:
            current_unchecked_block.append(line)

    flush_current_block()
    return markers
