from __future__ import annotations

import ast
import importlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_setup_tools_imports_public_cli_bridge_symbols() -> None:
    """Keep MCP setup tools off private CLI implementation names."""
    module_path = _REPO_ROOT / "src" / "awf" / "mcp" / "setup_tools.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    private_cli_imports: list[str] = []
    uses_bridge = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == "awf.cli":
            uses_bridge = any(alias.name == "first_run_mcp_bridge" for alias in node.names)
        if node.module is None or not node.module.startswith("awf.cli."):
            continue
        private_cli_imports.extend(
            f"{node.module}.{alias.name}" for alias in node.names if alias.name.startswith("_")
        )

    assert uses_bridge is True
    assert private_cli_imports == []


def test_first_run_mcp_bridge_public_exports_import() -> None:
    """Catch CLI private-symbol drift before MCP server startup."""
    bridge = importlib.import_module("awf.cli.first_run_mcp_bridge")

    assert bridge.__all__
    for export_name in bridge.__all__:
        assert hasattr(bridge, export_name), export_name
