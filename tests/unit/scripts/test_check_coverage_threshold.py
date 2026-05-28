"""Tests for the GitHub Actions coverage threshold helper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_coverage_threshold.py"


def _write_coverage_xml(
    path: Path,
    *,
    lines_valid: int,
    lines_covered: int,
    branches_valid: int = 0,
    branches_covered: int = 0,
) -> None:
    """Write a minimal coverage.py XML totals document for checker tests."""
    path.write_text(
        (
            '<?xml version="1.0" ?>\n'
            "<coverage "
            f'lines-valid="{lines_valid}" '
            f'lines-covered="{lines_covered}" '
            f'branches-valid="{branches_valid}" '
            f'branches-covered="{branches_covered}" '
            'line-rate="0" branch-rate="0" version="test" />\n'
        ),
        encoding="utf-8",
    )


def _run_checker(path: Path, minimum: str = "99") -> subprocess.CompletedProcess[str]:
    """Run the coverage threshold script in a subprocess."""
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(path),
            "--minimum-percent",
            minimum,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("minimum", ["-1", "100.1", "nan", "inf"])
def test_checker_rejects_invalid_minimum_percent_values(
    tmp_path: Path,
    minimum: str,
) -> None:
    """Invalid threshold values fail before any coverage totals are loaded."""
    coverage_xml = tmp_path / "coverage.xml"
    _write_coverage_xml(coverage_xml, lines_valid=100, lines_covered=100)

    result = _run_checker(coverage_xml, minimum)

    assert result.returncode == 2
    assert (
        "::error title=Coverage threshold invalid::"
        "argument --minimum-percent: "
        "minimum percent must be a finite value from 0 to 100"
    ) in result.stderr
    assert "Coverage totals:" not in result.stdout


@pytest.mark.unit
def test_checker_passes_when_combined_coverage_meets_threshold(tmp_path: Path) -> None:
    """A report at the configured combined threshold exits successfully."""
    coverage_xml = tmp_path / "coverage.xml"
    _write_coverage_xml(
        coverage_xml,
        lines_valid=100,
        lines_covered=99,
        branches_valid=100,
        branches_covered=99,
    )

    result = _run_checker(coverage_xml)

    assert result.returncode == 0
    assert "combined=99.00%" in result.stdout
    assert "Required minimum: 99.00%" in result.stdout


@pytest.mark.unit
def test_checker_fails_when_combined_line_and_branch_coverage_is_below_threshold(
    tmp_path: Path,
) -> None:
    """Combined line and branch coverage below the threshold emits an error."""
    coverage_xml = tmp_path / "coverage.xml"
    _write_coverage_xml(
        coverage_xml,
        lines_valid=42659,
        lines_covered=42402,
        branches_valid=13004,
        branches_covered=12633,
    )

    result = _run_checker(coverage_xml)

    assert result.returncode == 1
    assert "combined=98.87%" in result.stdout
    assert "::error title=Coverage below required threshold::" in result.stderr
    assert "Combined line+branch coverage 98.87% is below required 99.00%" in result.stderr


@pytest.mark.unit
def test_checker_uses_branch_totals_when_line_rate_alone_is_above_threshold(
    tmp_path: Path,
) -> None:
    """Branch totals contribute to the combined threshold decision."""
    coverage_xml = tmp_path / "coverage.xml"
    _write_coverage_xml(
        coverage_xml,
        lines_valid=1000,
        lines_covered=995,
        branches_valid=1000,
        branches_covered=970,
    )

    result = _run_checker(coverage_xml)

    assert result.returncode == 1
    assert "line=99.50%" in result.stdout
    assert "branch=97.00%" in result.stdout
    assert "combined=98.25%" in result.stdout


@pytest.mark.unit
def test_checker_reports_branch_only_coverage_without_traceback(tmp_path: Path) -> None:
    """Reports with no line opportunities use branch-only coverage cleanly."""
    coverage_xml = tmp_path / "coverage.xml"
    _write_coverage_xml(
        coverage_xml,
        lines_valid=0,
        lines_covered=0,
        branches_valid=100,
        branches_covered=99,
    )

    result = _run_checker(coverage_xml)

    assert result.returncode == 0
    assert "combined=99.00%" in result.stdout
    assert "line=n/a" in result.stdout
    assert "branch=99.00%" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_checker_reports_invalid_coverage_xml(tmp_path: Path) -> None:
    """Malformed or incomplete XML produces a GitHub Actions error."""
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text("<coverage lines-valid='10' />", encoding="utf-8")

    result = _run_checker(coverage_xml)

    assert result.returncode == 2
    assert "::error title=Coverage report invalid::" in result.stderr
    assert "missing root attribute" in result.stderr
