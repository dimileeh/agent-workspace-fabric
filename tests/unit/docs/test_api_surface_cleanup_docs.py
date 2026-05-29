"""Documentation tests for public API surface cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

PUBLIC_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/CONCEPTS.md",
    "docs/CLIENT_SURFACES.md",
    "docs/REST_API_REFERENCE.md",
    "docs/MCP_REFERENCE.md",
    "docs/MCP_CLIENT_PARITY.md",
    "docs/PR_MONITOR_ADOPTION.md",
    "docs/CLI_REFERENCE.md",
    "docs/README.md",
)

RETIRED_OPERATOR_SCRIPTS = (
    "scripts/run_awf.py",
    "scripts/attach_feature_pr_monitor.py",
    "scripts/release_pr_watcher.py",
    "scripts/schedule_release_pr.py",
    "scripts/remonitor_workspace.py",
    "scripts/salvage_workspace.py",
)

SUPPORTED_GENERATOR_SCRIPTS = {
    "generate_install_manifest.py",
    "generate_openapi.py",
    "generate_reason_catalog.py",
}


def _public_docs_text() -> str:
    """Return concatenated public docs text for surface assertions."""
    chunks: list[str] = []
    for relative_path in PUBLIC_DOCS:
        path = REPO_ROOT / relative_path
        assert path.exists(), f"public doc missing: {relative_path}"
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(chunks)


@pytest.mark.unit
def test_public_docs_publish_single_workspace_create_surface() -> None:
    """Public docs expose only the current workspace creation surface."""
    docs = _public_docs_text()

    assert "POST /v1/workspaces" in docs
    assert "awf_create_workspace" in docs
    assert "awf workspace create" in docs

    for retired in (
        "/v2/workspaces",
        "awf_create_workspace_v2",
        "Workspace create v2",
        "Workspace create v1",
        "Create a v2 workspace",
        "Create a v1 workspace",
    ):
        assert retired not in docs


@pytest.mark.unit
def test_public_docs_do_not_recommend_retired_operator_scripts() -> None:
    """Public docs do not direct operators to retired standalone scripts."""
    docs = _public_docs_text()

    for script in RETIRED_OPERATOR_SCRIPTS:
        assert script not in docs
    assert "awf-watchdog" not in docs


@pytest.mark.unit
def test_scripts_directory_contains_only_supported_generators() -> None:
    """The scripts directory keeps only supported generator entrypoints."""
    scripts = {
        path.name
        for path in (REPO_ROOT / "scripts").iterdir()
        if path.is_file() and path.name != "__init__.py"
    }

    assert scripts == SUPPORTED_GENERATOR_SCRIPTS
