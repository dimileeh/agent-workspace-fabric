"""Checksum verification aborts before any install mutation."""

from __future__ import annotations

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_checksum_mismatch_aborts_before_install(harness: InstallerHarness) -> None:
    """A wrong manifest sha256 aborts with the reason token and artifact URL."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, _digest = harness.write_wheel()
    wrong_sha = "b" * 64
    manifest = harness.write_manifest(wheel=wheel, sha256=wrong_sha)

    result = harness.run([], manifest=manifest)

    assert result.returncode != 0
    assert "CHECKSUM_MISMATCH" in result.stderr
    assert f"file://{wheel}" in result.stderr
    # The install method must not run once the checksum fails to match.
    assert "uv tool install" not in "\n".join(harness.calls())


@pytest.mark.unit
def test_checksum_mismatch_aborts_even_in_dry_run(harness: InstallerHarness) -> None:
    """Dry-run still verifies the checksum and refuses a tampered artifact."""
    harness.add_uname("Linux", "x86_64")
    harness.add_uv()
    wheel, _digest = harness.write_wheel()
    manifest = harness.write_manifest(wheel=wheel, sha256="c" * 64)

    result = harness.run(["--dry-run"], manifest=manifest)

    assert result.returncode != 0
    assert "CHECKSUM_MISMATCH" in result.stderr
