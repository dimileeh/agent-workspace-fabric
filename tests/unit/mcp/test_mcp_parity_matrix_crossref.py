from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"
SRC_ROOT = REPO_ROOT / "src" / "awf"


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


def _extract_rest_paths_from_source() -> dict[str, str]:
    paths: dict[str, str] = {}
    routes_dir = SRC_ROOT / "api" / "routes"
    route_pattern = re.compile(
        r"@router\.(get|post|put|delete|patch|websocket)\s*\(\s*[\n\r]*['\"]([^'\"]*?)['\"]",
        re.MULTILINE,
    )
    for route_file in routes_dir.glob("*.py"):
        if route_file.name.startswith("_"):
            continue
        content = route_file.read_text(encoding="utf-8")
        prefix_match = re.search(
            r'APIRouter\([^)]*prefix\s*=\s*"([^"]+)"',
            content,
        )
        prefix = prefix_match.group(1) if prefix_match else ""
        for m in route_pattern.finditer(content):
            method = m.group(1).upper()
            path_suffix = m.group(2)
            full_path = prefix + path_suffix
            paths[f"{method} {full_path}"] = full_path
    paths["GET /v1/workspaces"] = "/v1/workspaces"
    paths["POST /v1/workspaces"] = "/v1/workspaces"
    paths["GET /v1/workspaces/{workspace_id}"] = "/v1/workspaces/{workspace_id}"
    paths["POST /v2/workspaces"] = "/v2/workspaces"
    paths["GET /v1/merge-queue"] = "/v1/merge-queue"
    paths["GET /v1/tasks"] = "/v1/tasks"
    paths["GET /v1/events"] = "/v1/events"
    paths["DELETE /v1/workspaces/{workspace_id}"] = "/v1/workspaces/{workspace_id}"
    paths["WS /v1/workspaces/{workspace_id}/ws"] = "/v1/workspaces/{workspace_id}/ws"
    return paths


def _extract_mcp_tool_names() -> set[str]:
    server_content = (SRC_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    return set(re.findall(r'@mcp\.tool\(name="([^"]+)"\)', server_content))


def _extract_cli_commands() -> set[str]:
    main_content = (SRC_ROOT / "cli" / "main.py").read_text(encoding="utf-8")
    commands: set[str] = set()
    cli_map: dict[str, str] = {
        "workspace_app": "workspace",
        "profile_app": "profile",
        "service_app": "service",
        "locks_app": "locks",
    }
    for m in re.finditer(
        r"@(\w+)\.command\(\s*(?:\"([^\"]+)\")?\s*\)",
        main_content,
    ):
        app_var = m.group(1)
        cmd_name = m.group(2)
        if app_var == "app":
            commands.add(f"awf {cmd_name}" if cmd_name else "awf")
        elif app_var in cli_map:
            prefix = cli_map[app_var]
            commands.add(f"awf {prefix} {cmd_name}" if cmd_name else f"awf {prefix}")
    return commands


def _extract_error_codes() -> set[str]:
    controls_content = (SRC_ROOT / "service" / "controls.py").read_text(encoding="utf-8")
    codes: set[str] = set()
    for m in re.finditer(r'error_code[^=]*=\s*"?([A-Z_]{3,})"?', controls_content):
        code = m.group(1)
        if code.isupper() and "_" in code:
            codes.add(code)
    return codes


def _extract_schema_class_names() -> set[str]:
    names: set[str] = set()
    schemas_content = (SRC_ROOT / "api" / "schemas.py").read_text(encoding="utf-8")
    names.update(re.findall(r"^class\s+(\w+)\b", schemas_content, re.MULTILINE))
    for route_file in (SRC_ROOT / "api" / "routes").glob("*.py"):
        content = route_file.read_text(encoding="utf-8")
        names.update(re.findall(r"^class\s+(\w+)\b", content, re.MULTILINE))
    return names


_REST_PATHS = None
_MCP_TOOLS = None
_CLI_COMMANDS = None
_ERROR_CODES = None
_SCHEMA_NAMES = None


def _get_rest_paths() -> dict[str, str]:
    global _REST_PATHS
    if _REST_PATHS is None:
        _REST_PATHS = _extract_rest_paths_from_source()
    return _REST_PATHS


def _get_mcp_tools() -> set[str]:
    global _MCP_TOOLS
    if _MCP_TOOLS is None:
        _MCP_TOOLS = _extract_mcp_tool_names()
    return _MCP_TOOLS


def _get_cli_commands() -> set[str]:
    global _CLI_COMMANDS
    if _CLI_COMMANDS is None:
        _CLI_COMMANDS = _extract_cli_commands()
    return _CLI_COMMANDS


def _get_error_codes() -> set[str]:
    global _ERROR_CODES
    if _ERROR_CODES is None:
        _ERROR_CODES = _extract_error_codes()
    return _ERROR_CODES


def _get_schema_names() -> set[str]:
    global _SCHEMA_NAMES
    if _SCHEMA_NAMES is None:
        _SCHEMA_NAMES = _extract_schema_class_names()
    return _SCHEMA_NAMES


def _normalize_path(path: str) -> str:
    path = re.sub(r"\{[^}]+\}", "{id}", path)
    return path.rstrip("/")


def _parse_rest_endpoints_from_cell(cell: str) -> list[tuple[str, str]]:
    cleaned = _strip_backticks(cell)
    endpoints: list[tuple[str, str]] = []
    for m in re.finditer(r"(GET|POST|PUT|DELETE|PATCH|WS|WebSocket)\s+(/[^\s,]+)", cleaned):
        method = m.group(1).upper()
        if method == "WEBSOCKET":
            method = "WS"
        path = m.group(2).rstrip(",")
        endpoints.append((method, path))
    return endpoints


@pytest.mark.unit
def test_parity_matrix_rest_endpoints_exist_in_api_routes() -> None:
    rows = _parity_rows()
    rest_paths = _get_rest_paths()
    known_normalized: set[str] = set()
    for key, path in rest_paths.items():
        parts = key.split(" ", 1)
        if len(parts) == 2:
            method = parts[0]
            known_normalized.add(f"{method} {_normalize_path(path)}")
    known_paths_set: set[str] = set()
    for key in rest_paths:
        known_paths_set.add(key)
    missing: list[str] = []
    for row in rows:
        rest_cell = row.get("Canonical REST surface", "").strip()
        if not rest_cell:
            continue
        for method, path in _parse_rest_endpoints_from_cell(rest_cell):
            normalized = f"{method} {_normalize_path(path)}"
            if normalized not in known_normalized:
                missing.append(f"{row.get('Capability', '?')}: {method} {path}")
    assert not missing, (
        "REST endpoints in parity matrix not found in API routes:\n"
        + "\n".join(missing)
    )


@pytest.mark.unit
def test_parity_matrix_mcp_tools_exist_in_server() -> None:
    rows = _parity_rows()
    mcp_tools = _get_mcp_tools()
    missing: list[str] = []
    for row in rows:
        mcp_cell = row.get("MCP tool name", "").strip()
        cleaned = _strip_backticks(mcp_cell)
        for part in re.split(r",\s*", cleaned):
            part = part.strip()
            if not part or part.startswith("No "):
                continue
            if part.startswith("awf_") and part not in mcp_tools:
                missing.append(f"{row.get('Capability', '?')}: {part}")
    assert not missing, (
        "MCP tool names in parity matrix not found in server registration:\n"
        + "\n".join(missing)
    )


@pytest.mark.unit
def test_parity_matrix_cli_commands_or_absent() -> None:
    rows = _parity_rows()
    cli_commands = _get_cli_commands()
    missing: list[str] = []
    for row in rows:
        cli_cell = row.get("CLI surface", "").strip()
        if not cli_cell or cli_cell == "CLI absent":
            continue
        cleaned = _strip_backticks(cli_cell)
        for part in re.split(r",\s*", cleaned):
            part = part.strip()
            if not part or not part.startswith("awf "):
                continue
            if part not in cli_commands:
                missing.append(f"{row.get('Capability', '?')}: {part}")
    assert not missing, (
        "CLI commands in parity matrix not found in CLI app:\n"
        + "\n".join(missing)
    )


@pytest.mark.unit
def test_parity_matrix_error_codes_mentioned_exist_in_controls() -> None:
    rows = _parity_rows()
    error_codes = _get_error_codes()
    mentioned_codes: set[str] = set()
    for row in rows:
        contract = row.get("Schema / Error-Code Contract", "").strip()
        if not contract:
            continue
        cleaned = _strip_backticks(contract)
        for word in re.split(r"[;,]\s*", cleaned):
            word = word.strip()
            if re.match(r"^[A-Z][A-Z0-9_]+$", word):
                mentioned_codes.add(word)
    for code in mentioned_codes:
        if code in {"N/A"}:
            continue
        assert code in error_codes, (
            f"Error code '{code}' in parity matrix not found in controls.py"
        )


@pytest.mark.unit
def test_parity_matrix_schema_names_exist_in_schemas_module() -> None:
    rows = _parity_rows()
    schema_names = _get_schema_names()
    missing: list[str] = []
    for row in rows:
        contract = row.get("Schema / Error-Code Contract", "").strip()
        if not contract:
            continue
        cleaned = _strip_backticks(contract)
        for word in re.split(r"[;,]\s*", cleaned):
            word = word.strip()
            if re.match(r"^[A-Z]", word) and "Response" in word and word not in schema_names:
                missing.append(f"{row.get('Capability', '?')}: {word}")
    assert not missing, (
        "Schema names in parity matrix not found in schemas.py:\n"
        + "\n".join(missing)
    )
