#!/usr/bin/env python3
"""Fail CI when coverage.xml is below the configured combined threshold."""

from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageTotals:
    """Line and branch coverage totals parsed from a coverage.py XML report."""

    lines_valid: int
    lines_covered: int
    branches_valid: int
    branches_covered: int

    @property
    def valid_total(self) -> int:
        """Return the total measurable line and branch opportunities."""
        return self.lines_valid + self.branches_valid

    @property
    def covered_total(self) -> int:
        """Return the total covered line and branch opportunities."""
        return self.lines_covered + self.branches_covered

    @property
    def line_percent(self) -> float:
        """Return line coverage as a percentage."""
        return _percent(self.lines_covered, self.lines_valid)

    @property
    def branch_percent(self) -> float | None:
        """Return branch coverage as a percentage when branch data exists."""
        if self.branches_valid == 0:
            return None
        return _percent(self.branches_covered, self.branches_valid)

    @property
    def combined_percent(self) -> float:
        """Return combined line and branch coverage as a percentage."""
        return _percent(self.covered_total, self.valid_total)


def _percent(covered: int, valid: int) -> float:
    """Calculate a percentage while rejecting empty measurable totals."""
    if valid <= 0:
        raise ValueError("coverage report has no measurable line or branch opportunities")
    return covered / valid * 100.0


def _required_int(root: ET.Element, attr: str) -> int:
    """Read a required non-negative integer attribute from the XML root."""
    raw = root.attrib.get(attr)
    if raw is None:
        raise ValueError(f"coverage XML is missing root attribute {attr!r}")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"coverage XML root attribute {attr!r} is not an integer") from exc
    if value < 0:
        raise ValueError(f"coverage XML root attribute {attr!r} must be non-negative")
    return value


def load_coverage_totals(path: Path) -> CoverageTotals:
    """Load and validate aggregate line and branch totals from coverage XML."""
    root = ET.parse(path).getroot()
    totals = CoverageTotals(
        lines_valid=_required_int(root, "lines-valid"),
        lines_covered=_required_int(root, "lines-covered"),
        branches_valid=_required_int(root, "branches-valid"),
        branches_covered=_required_int(root, "branches-covered"),
    )
    if totals.lines_covered > totals.lines_valid:
        raise ValueError("coverage XML has more covered lines than valid lines")
    if totals.branches_covered > totals.branches_valid:
        raise ValueError("coverage XML has more covered branches than valid branches")
    if totals.valid_total <= 0:
        raise ValueError("coverage XML has no measurable line or branch opportunities")
    return totals


def _format_optional_percent(value: float | None) -> str:
    """Format an optional coverage percentage for CLI output."""
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def _minimum_percent_arg(raw_value: str) -> float:
    """Parse the minimum percentage CLI argument with explicit bounds."""
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "minimum percent must be a finite value from 0 to 100"
        ) from exc
    if not math.isfinite(value) or value < 0.0 or value > 100.0:
        raise argparse.ArgumentTypeError("minimum percent must be a finite value from 0 to 100")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Run the coverage threshold checker CLI."""
    parser = argparse.ArgumentParser(description=__doc__, exit_on_error=False)
    parser.add_argument("coverage_xml", type=Path)
    parser.add_argument("--minimum-percent", type=_minimum_percent_arg, required=True)
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as exc:
        print(f"::error title=Coverage threshold invalid::{exc}", file=sys.stderr)
        return 2
    minimum_percent = args.minimum_percent

    try:
        totals = load_coverage_totals(args.coverage_xml)
    except (OSError, ET.ParseError, ValueError) as exc:
        print(
            "::error title=Coverage report invalid::"
            f"Unable to read coverage totals from {args.coverage_xml}: {exc}",
            file=sys.stderr,
        )
        return 2

    combined_percent = totals.combined_percent
    branch_percent = totals.branch_percent
    line_percent: float | None = None if totals.lines_valid == 0 else totals.line_percent
    print(
        "Coverage totals: "
        f"combined={combined_percent:.2f}% "
        f"line={_format_optional_percent(line_percent)} "
        f"branch={_format_optional_percent(branch_percent)} "
        f"covered={totals.covered_total}/{totals.valid_total}"
    )
    print(f"Required minimum: {minimum_percent:.2f}%")

    if combined_percent + 1e-9 < minimum_percent:
        sys.stdout.flush()
        print(
            "::error title=Coverage below required threshold::"
            f"Combined line+branch coverage {combined_percent:.2f}% is below "
            f"required {minimum_percent:.2f}%. Add meaningful tests without "
            "lowering coverage thresholds.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
