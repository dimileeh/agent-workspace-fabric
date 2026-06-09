"""Syntax and ``--help`` contract tests for ``packaging/uninstall.sh`` (T21)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.installer.conftest import UNINSTALLER, InstallerHarness

DOCUMENTED_FLAGS = (
    "--dry-run",
    "--non-interactive",
    "--yes",
    "--install-dir",
    "--purge-config",
    "--purge-state",
    "--remove-uv",
    "--all",
    "--shell",
    "--help",
)


@pytest.mark.unit
def test_uninstall_sh_is_a_checked_in_executable(uninstaller_path: Path) -> None:
    """The uninstaller ships checked-in with a bash shebang and strict mode."""
    assert uninstaller_path.is_file()
    text = uninstaller_path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


@pytest.mark.unit
def test_uninstall_sh_passes_bash_syntax_check(uninstaller_path: Path) -> None:
    """``bash -n`` parses the uninstaller without errors."""
    result = subprocess.run(
        ["bash", "-n", str(uninstaller_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.unit
def test_help_lists_every_documented_flag(harness: InstallerHarness) -> None:
    """``--help`` exits 0, lists every flag, and performs no mutation."""
    result = harness.run(["--help"], script=UNINSTALLER)

    assert result.returncode == 0, result.stderr
    for flag in DOCUMENTED_FLAGS:
        assert flag in result.stdout, flag
    assert harness.calls() == []


@pytest.mark.unit
def test_help_documents_credential_preservation(harness: InstallerHarness) -> None:
    """``--help`` states credentials are preserved by default (the core contract)."""
    result = harness.run(["--help"], script=UNINSTALLER)

    assert result.returncode == 0, result.stderr
    # The trust paragraph promises credential preservation; assert on the stable
    # keyword, not the exact prose.
    assert "credential" in result.stdout.lower()


@pytest.mark.unit
def test_unknown_flag_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An unknown flag fails fast with ``BAD_USAGE`` and a non-zero exit."""
    result = harness.run(["--does-not-exist"], script=UNINSTALLER)

    assert result.returncode == 2
    assert "BAD_USAGE" in result.stderr


@pytest.mark.unit
def test_empty_install_dir_is_a_bad_usage_error(harness: InstallerHarness) -> None:
    """An explicit empty ``--install-dir`` is rejected, mirroring the installer."""
    result = harness.run(["--install-dir", ""], script=UNINSTALLER)

    assert result.returncode == 2
    assert "BAD_USAGE" in result.stderr
