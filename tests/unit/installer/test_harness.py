"""Regression coverage for installer test harness behavior."""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.installer.conftest import InstallerHarness


@pytest.mark.unit
def test_awf_stub_escapes_single_quote_in_version(harness: InstallerHarness) -> None:
    """A quoted version token still produces a valid ``awf --version`` stub."""
    awf = harness.add_awf(version="0.1.0'foo")

    result = subprocess.run(
        [awf, "--version"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "awf 0.1.0'foo\n"
