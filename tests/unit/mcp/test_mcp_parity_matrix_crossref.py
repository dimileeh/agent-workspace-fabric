from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from tests.unit.mcp._parity_utils import (
    MISSING_STATUS,
    PARTIAL_STATUS,
    TODO_DOC,
    _extract_cli_invocations_from_cell,
    _extract_mcp_tool_tokens_from_cell,
    _extract_rest_endpoints_from_cell,
    _is_implemented_row,
    _is_partial_or_missing_row,
    _normalize_cell,
    _parity_backlog_slice,
    _parity_rows,
    _parity_status,
    _rest_cell_names_endpoints,
    _strip_backticks,
    _unchecked_todo_markers,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "awf"


def _extract_rest_paths_from_app() -> dict[str, str]:
    from awf.api.app import create_app

    app = create_app(use_lifespan=False)
    paths: dict[str, str] = {}
    for route in app.routes:
        if isinstance(route, APIWebSocketRoute):
            paths[f"WS {route.path}"] = route.path
        elif isinstance(route, APIRoute):
            for method in route.methods or set():
                paths[f"{method} {route.path}"] = route.path
    return paths


def _extract_mcp_tool_names() -> set[str]:
    tool_names: set[str] = set()
    for mcp_file in (SRC_ROOT / "mcp").glob("*.py"):
        content = mcp_file.read_text(encoding="utf-8")
        tool_names.update(re.findall(r'@mcp\.tool\(name="([^"]+)"\)', content))
    return tool_names


def _extract_cli_commands() -> set[str]:
    from typer.main import get_command

    from awf.cli.main import app

    grp = get_command(app)

    def _walk(grp: object, prefix: str) -> set[str]:
        result: set[str] = set()
        for name, cmd in getattr(grp, "commands", {}).items():
            full = f"{prefix} {name}".replace("  ", " ")
            if hasattr(cmd, "commands"):
                result.update(_walk(cmd, full))
            else:
                result.add(full)
        return result

    return _walk(grp, "awf")


def _extract_cli_flags() -> dict[str, set[str]]:
    from typer.main import get_command

    from awf.cli.main import app

    grp = get_command(app)

    def _walk(grp: object, prefix: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for name, cmd in getattr(grp, "commands", {}).items():
            full = f"{prefix} {name}".replace("  ", " ")
            if hasattr(cmd, "commands"):
                result.update(_walk(cmd, full))
            else:
                flags: set[str] = set()
                for param in getattr(cmd, "params", []):
                    for opt in getattr(param, "opts", ()):
                        if opt.startswith("--"):
                            flags.add(opt)
                result[full] = flags
        return result

    return _walk(grp, "awf")


def _extract_error_codes() -> set[str]:
    codes: set[str] = set()
    source_dirs = [
        SRC_ROOT / "service",
        SRC_ROOT / "api" / "routes",
        SRC_ROOT / "runtime",
        SRC_ROOT / "control",
        SRC_ROOT / "mcp",
    ]
    for source_dir in source_dirs:
        if not source_dir.is_dir():
            continue
        for py_file in source_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            for m in re.finditer(r'error_code[^=]*=\s*"?([A-Z_]{3,})"?', content):
                code = m.group(1)
                if code.isupper() and "_" in code:
                    codes.add(code)
            for m in re.finditer(r'"error_code":\s*"([A-Z_]+)"', content):
                codes.add(m.group(1))
            for m in re.finditer(r'error_code="([A-Z_]+)"', content):
                codes.add(m.group(1))
    return codes


FRAMEWORK_RESPONSE_TYPES = {
    "FileResponse",
    "StreamingResponse",
    "JSONResponse",
    "HTMLResponse",
    "RedirectResponse",
    "Response",
}


def _extract_schema_class_names() -> set[str]:
    names: set[str] = set()
    for schema_file in (SRC_ROOT / "api").glob("schemas*.py"):
        schemas_content = schema_file.read_text(encoding="utf-8")
        names.update(re.findall(r"^class\s+(\w+)\b", schemas_content, re.MULTILINE))
    for route_file in (SRC_ROOT / "api" / "routes").glob("*.py"):
        content = route_file.read_text(encoding="utf-8")
        names.update(re.findall(r"^class\s+(\w+)\b", content, re.MULTILINE))
    return names


_REST_PATHS = None
_MCP_TOOLS = None
_CLI_COMMANDS = None
_CLI_FLAGS = None
_ERROR_CODES = None
_SCHEMA_NAMES = None


def _get_rest_paths() -> dict[str, str]:
    global _REST_PATHS
    if _REST_PATHS is None:
        _REST_PATHS = _extract_rest_paths_from_app()
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


def _get_cli_flags() -> dict[str, set[str]]:
    global _CLI_FLAGS
    if _CLI_FLAGS is None:
        _CLI_FLAGS = _extract_cli_flags()
    return _CLI_FLAGS


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


def _is_local_first_run_contract(rest_cell: str) -> bool:
    return _normalize_cell(rest_cell) == "Local first-run setup contract (REST unchanged)"


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
        parsed = _extract_rest_endpoints_from_cell(rest_cell)
        looks_like_endpoints = bool(
            re.search(r"(GET|POST|PUT|DELETE|PATCH|WS|WebSocket)\s+/", _strip_backticks(rest_cell))
        )
        assert not looks_like_endpoints or parsed, (
            f"{row.get('Capability', '?')}: non-empty REST cell looks like it should list endpoints but none could be parsed: {rest_cell!r}"
        )
        for method, path in parsed:
            normalized = f"{method} {_normalize_path(path)}"
            if normalized not in known_normalized:
                missing.append(f"{row.get('Capability', '?')}: {method} {path}")
    assert not missing, "REST endpoints in parity matrix not found in API routes:\n" + "\n".join(
        missing
    )


@pytest.mark.unit
def test_parity_matrix_implemented_status_is_backed_by_real_surfaces() -> None:
    rows = _parity_rows()
    rest_paths = _get_rest_paths()
    known_rest = {
        f"{key.split(' ', 1)[0]} {_normalize_path(path)}"
        for key, path in rest_paths.items()
        if " " in key
    }
    mcp_tools = _get_mcp_tools()
    cli_commands = _get_cli_commands()
    cli_flags = _get_cli_flags()

    failures: list[str] = []
    for row in rows:
        if not _is_implemented_row(row):
            continue

        capability = row.get("Capability", "?")
        rest_cell = row.get("Canonical REST surface", "")
        rest_endpoints = _extract_rest_endpoints_from_cell(rest_cell)
        if _rest_cell_names_endpoints(rest_cell):
            if not rest_endpoints:
                failures.append(f"{capability}: implemented row has no parseable REST endpoint")
            for method, path in rest_endpoints:
                normalized = f"{method} {_normalize_path(path)}"
                if normalized not in known_rest:
                    failures.append(
                        f"{capability}: REST endpoint {method} {path} is not registered"
                    )
        elif not (
            _normalize_cell(rest_cell).startswith("If-Match")
            or _is_local_first_run_contract(rest_cell)
        ):
            failures.append(
                f"{capability}: implemented row must name REST endpoints or a documented cross-cutting REST contract"
            )

        mcp_tool_names = _extract_mcp_tool_tokens_from_cell(row.get("MCP tool name", ""))
        if not mcp_tool_names:
            failures.append(f"{capability}: implemented row declares no concrete MCP awf_* tool")
        for tool_name in mcp_tool_names:
            if tool_name not in mcp_tools:
                failures.append(f"{capability}: MCP tool {tool_name} is not registered")

        backlog = _parity_backlog_slice(row)
        if backlog.startswith("TODO§"):
            failures.append(
                f"{capability}: implemented row still points at active backlog {backlog}"
            )

        cli_cell = row.get("CLI surface", "")
        if _normalize_cell(cli_cell) == "CLI absent":
            continue

        invocations = _extract_cli_invocations_from_cell(cli_cell)
        if not invocations:
            failures.append(
                f"{capability}: implemented row must list CLI commands or exactly 'CLI absent'"
            )
            continue
        for invocation in invocations:
            if not invocation.command.startswith("awf "):
                failures.append(
                    f"{capability}: CLI entry {invocation.source!r} is not an awf command"
                )
                continue
            if invocation.command not in cli_commands:
                failures.append(
                    f"{capability}: CLI command {invocation.command!r} is not registered"
                )
                continue
            allowed_flags = cli_flags.get(invocation.command, set())
            for flag in invocation.flags:
                if flag not in allowed_flags:
                    failures.append(
                        f"{capability}: CLI entry {invocation.source!r} documents unsupported flag {flag}"
                    )

    assert not failures, "Implemented parity-matrix rows drifted from real surfaces:\n" + "\n".join(
        failures
    )


@pytest.mark.unit
def test_partial_and_missing_rows_have_unchecked_backlog_visibility() -> None:
    rows = _parity_rows()
    active_todo_markers = (
        _unchecked_todo_markers(TODO_DOC.read_text(encoding="utf-8"))
        if TODO_DOC.exists()
        else set()
    )

    failures: list[str] = []
    for row in rows:
        status = _parity_status(row)
        if not _is_partial_or_missing_row(row):
            continue

        capability = row.get("Capability", "?")
        backlog = _parity_backlog_slice(row)
        if not backlog.startswith("TODO§"):
            failures.append(
                f"{capability}: {status} row must use TODO§... Backlog Slice, got {backlog!r}"
            )
            continue
        if backlog not in active_todo_markers:
            failures.append(
                f"{capability}: backlog marker {backlog} is not present in an unchecked TODO item"
            )
        if status == MISSING_STATUS and _extract_mcp_tool_tokens_from_cell(
            row.get("MCP tool name", "")
        ):
            failures.append(f"{capability}: missing/backlog row must not declare a live MCP tool")
        if status == PARTIAL_STATUS and not _extract_mcp_tool_tokens_from_cell(
            row.get("MCP tool name", "")
        ):
            failures.append(f"{capability}: partial row must declare the implemented MCP subset")

    assert not failures, (
        "Non-implemented parity-matrix rows lost backlog visibility:\n" + "\n".join(failures)
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
        "MCP tool names in parity matrix not found in server registration:\n" + "\n".join(missing)
    )


@pytest.mark.unit
def test_parity_matrix_cli_commands_or_absent() -> None:
    rows = _parity_rows()
    cli_commands = _get_cli_commands()
    cli_flags = _get_cli_flags()
    missing: list[str] = []
    for row in rows:
        cli_cell = row.get("CLI surface", "").strip()
        if not cli_cell or cli_cell == "CLI absent":
            continue
        cleaned = _strip_backticks(cli_cell)
        for part in re.split(r",\s*", cleaned):
            part = part.strip()
            if not part:
                continue
            if not part.startswith("awf "):
                missing.append(f"{row.get('Capability', '?')}: {part} (malformed CLI entry)")
                continue
            tokens = part.split()
            base_end = len(tokens)
            for i, t in enumerate(tokens):
                if t.startswith("--"):
                    base_end = i
                    break
            base_cmd = " ".join(tokens[:base_end])
            if base_cmd not in cli_commands:
                missing.append(f"{row.get('Capability', '?')}: {part}")
                continue
            allowed = cli_flags.get(base_cmd, set())
            for token in tokens:
                if not token.startswith("--"):
                    continue
                flag_name = token.split("=")[0]
                if flag_name not in allowed:
                    missing.append(
                        f"{row.get('Capability', '?')}: {part} (unsupported flag {flag_name})"
                    )
    assert not missing, "CLI commands/flags in parity matrix not found in CLI app:\n" + "\n".join(
        missing
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
            f"Error code '{code}' in parity matrix not found in source modules"
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
                if word in FRAMEWORK_RESPONSE_TYPES:
                    continue
                missing.append(f"{row.get('Capability', '?')}: {word}")
    assert not missing, "Schema names in parity matrix not found in schemas.py:\n" + "\n".join(
        missing
    )
