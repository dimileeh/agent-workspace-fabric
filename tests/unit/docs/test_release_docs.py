"""Documentation tests for the AWF release manifest contract."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASING_PATH = REPO_ROOT / "RELEASING.md"


@pytest.mark.unit
def test_releasing_docs_explain_manifest_inspection_and_verification() -> None:
    """Release docs describe manifest inspection and immutable verification."""
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


@pytest.mark.unit
def test_releasing_docs_explain_manual_artifact_drift_and_installer_smoke() -> None:
    """Release docs explain how to verify artifacts manually (drift + installer smoke)."""
    docs = RELEASING_PATH.read_text(encoding="utf-8")

    assert "scripts/check_release_artifacts.py" in docs
    assert "scripts/release_smoke.py" in docs
    assert "before install" in docs
    assert "Checksum verified" in docs


@pytest.mark.unit
def test_releasing_docs_require_release_asset_publication_before_manifest_use() -> None:
    """Manifest docs require published GitHub Release assets before consumption."""
    docs = RELEASING_PATH.read_text(encoding="utf-8")

    assert "GitHub Release assets" in docs
    assert "Actions artifacts" in docs
    assert "not served from" in docs
    assert "`/releases/download/`" in docs
    assert "gh release upload" in docs
    assert "curl --fail --head --location" in docs
    assert "Do not publish, advertise, or let installers consume" in docs
