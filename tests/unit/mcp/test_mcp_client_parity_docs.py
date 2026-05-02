from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"
README = REPO_ROOT / "README.md"


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
        "artifact content/download",
        "If-Match",
    ):
        assert missing_surface in doc

    assert "not arbitrary shell" in doc
    assert "unrestricted Docker exec" in doc
