"""Documentation tests for the AWF release manifest contract."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASING_PATH = REPO_ROOT / "RELEASING.md"


@pytest.mark.unit
def test_releasing_docs_explain_manifest_inspection_and_verification() -> None:
    docs = RELEASING_PATH.read_text(encoding="utf-8")

    assert "awf-install-manifest.json" in docs
    assert "python-distribution-sha256.txt" in docs
    assert "scripts/generate_install_manifest.py" in docs
    assert "stable" in docs
    assert "prerelease" in docs
    assert "auto" in docs
    assert "releases/download/vX.Y.Z" in docs
    assert "sha256sum -c artifacts/release/python-distribution-sha256.txt" in docs
    assert "jq" in docs
    assert "signatures" in docs
    assert "reserved" in docs
    assert "/latest/" not in docs
