"""Syntax and ``--help`` contract tests for ``packaging/install.sh``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.installer.conftest import InstallerHarness

DOCUMENTED_FLAGS = (
    "--version",
    "--channel",
    "--method",
    "--install-dir",
    "--dry-run",
    "--uninstall",
    "--shell",
    "--help",
)


@pytest.mark.unit
def test_install_sh_is_a_checked_in_executable(installer_path: Path) -> None:
    """The installer ships checked-in with a bash shebang and strict mode."""
    assert installer_path.is_file()
    text = installer_path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


@pytest.mark.unit
def test_install_sh_passes_bash_syntax_check(installer_path: Path) -> None:
    """``bash -n`` parses the installer without errors."""
    result = subprocess.run(
        ["bash", "-n", str(installer_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_help_lists_every_documented_flag(harness: InstallerHarness) -> None:
    """``--help`` exits 0, lists every flag, and performs no mutation."""
    result = harness.run(["--help"])

    assert result.returncode == 0, result.stderr
    for flag in DOCUMENTED_FLAGS:
        assert flag in result.stdout, flag
    assert harness.calls() == []


@pytest.mark.unit
def test_unknown_flag_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unknown flag fails fast with ``BAD_USAGE`` and a non-zero exit."""
    result = harness.run(["--does-not-exist"])

    assert result.returncode != 0
    assert "BAD_USAGE" in result.stderr
