"""Tests for pytest-cov term-missing gap metadata extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.validation import (
    ValidationCoverageResult,
    _missing_line_count,
    _parse_term_missing_gaps,
)


@pytest.mark.unit
def test_parse_term_missing_extracts_gaps_from_stdout(tmp_path: Path) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name                                      Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------------------------\n"
        "src/awf/control/executor.py                 800    100    88%   10-20, 50, 75-80\n"
        "src/awf/runtime/validation.py               400     20    95%   30-45\n"
        "src/awf/api/schemas.py                      200      5    98%   99\n"
        "---------------------------------------------------------------------\n"
        "TOTAL                                      1400    125    91%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 3
    assert gaps[0]["file"] == "src/awf/control/executor.py"
    assert gaps[0]["missing_lines"] == ["10-20", "50", "75-80"]
    assert gaps[1]["file"] == "src/awf/runtime/validation.py"
    assert gaps[1]["missing_lines"] == ["30-45"]
    assert gaps[2]["file"] == "src/awf/api/schemas.py"
    assert gaps[2]["missing_lines"] == ["99"]


@pytest.mark.unit
def test_parse_term_missing_handles_empty_output(tmp_path: Path) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name        Stmts   Miss  Cover\n"
        "-------------------------------\n"
        "TOTAL         100      5    95%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert gaps == []


@pytest.mark.unit
def test_parse_term_missing_handles_no_output(tmp_path: Path) -> None:
    gaps = _parse_term_missing_gaps([Path("/nonexistent/path")])
    assert gaps == []

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("")
    gaps = _parse_term_missing_gaps([empty_file])
    assert gaps == []


@pytest.mark.unit
def test_parse_term_missing_handles_truncated_output(tmp_path: Path) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name                                      Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------------------------\n"
        "src/awf/control/executor.py                 800    100    88%   10-2\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 1
    assert gaps[0]["file"] == "src/awf/control/executor.py"
    assert gaps[0]["missing_lines"] == ["10-2"]


@pytest.mark.unit
def test_parse_term_missing_handles_malformed_lines(tmp_path: Path) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "coverage preamble before the table\n"
        "Name                                      Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------------------------\n"
        "not enough columns here\n"
        "src/awf/control/executor.py                 800    100    88%   10-20\n"
        "---------------------------------------------------------------------\n"
        "TOTAL                                      1400    125    91%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 1
    assert gaps[0]["file"] == "src/awf/control/executor.py"
    assert gaps[0]["missing_lines"] == ["10-20"]


@pytest.mark.unit
def test_parse_term_missing_ignores_lines_before_header(tmp_path: Path) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "pytest started\n"
        "src/awf/before_header.py                 10      5    50%   1-5\n"
        "Name                                      Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------------------------\n"
        "src/awf/after_header.py                  100     10    90%   5-10\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert gaps == [{"file": "src/awf/after_header.py", "missing_lines": ["5-10"]}]


@pytest.mark.unit
def test_missing_line_count_handles_ranges_non_strings_and_malformed_tokens() -> None:
    assert _missing_line_count([10, None, "7-9", "12-nope", "abc", "14"]) == 6


@pytest.mark.unit
def test_parse_term_missing_sorts_by_most_missing_and_caps_at_ten(
    tmp_path: Path,
) -> None:
    lines = [
        "Name                                      Stmts   Miss  Cover   Missing\n",
        "---------------------------------------------------------------------\n",
    ]
    for i in range(15):
        missing_count = 100 - i * 5
        lines.append(
            f"src/pkg/module_{i:02d}.py"
            f"{' ' * (40 - len(f'src/pkg/module_{i:02d}.py'))}"
            f"  {200}    {missing_count}    {100 - missing_count // 2}%"
            f"   1-{missing_count}\n"
        )
    lines.append("---------------------------------------------------------------------\n")
    lines.append("TOTAL                                      3000    750    75%\n")
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text("".join(lines), encoding="utf-8")

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 10
    assert gaps[0]["file"] == "src/pkg/module_00.py"


@pytest.mark.unit
def test_parse_term_missing_sorts_by_line_count_not_tokens(
    tmp_path: Path,
) -> None:
    """Ranges count as many lines, not one token."""
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name                                      Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------------------------------\n"
        "src/awf/big_range.py                        200    191    4%   10-200\n"
        "src/awf/small_tokens.py                     200      4   98%   10, 11, 12, 13\n"
        "---------------------------------------------------------------------\n"
        "TOTAL                                      400    195    51%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 2
    assert gaps[0]["file"] == "src/awf/big_range.py"
    assert gaps[1]["file"] == "src/awf/small_tokens.py"


@pytest.mark.unit
def test_missing_line_count_treats_malformed_tokens_as_single_lines() -> None:
    assert _missing_line_count([None, "  7  ", "10-12", "bad-range", "x"]) == 6


@pytest.mark.unit
def test_coverage_result_metadata_includes_gaps(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/a.py", "missing_lines": ["10-20", "50"]},
        {"file": "src/b.py", "missing_lines": ["30-45"]},
    ]
    result = ValidationCoverageResult(
        provider="python",
        percent=88.0,
        minimum_percent=90.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        gaps=gaps,
    )

    metadata = result.as_metadata()

    assert metadata["gaps"] == gaps
    assert metadata["percent"] == 88.0
    assert metadata["reason_code"] == "COVERAGE_BELOW_THRESHOLD"


@pytest.mark.unit
def test_coverage_result_metadata_omits_gaps_when_empty(tmp_path: Path) -> None:
    result = ValidationCoverageResult(
        provider="python",
        percent=95.0,
        minimum_percent=90.0,
        enforce=True,
        status="passed",
        reason_code="COVERAGE_OK",
        gaps=[],
    )

    metadata = result.as_metadata()

    assert "gaps" not in metadata


@pytest.mark.unit
def test_parse_term_missing_handles_header_without_following_data(
    tmp_path: Path,
) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name        Stmts   Miss  Cover   Missing\n"
        "---------------------------------------------\n"
        "TOTAL         100      5    95%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert gaps == []


@pytest.mark.unit
def test_parse_term_missing_reads_from_multiple_files(
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "stdout.txt"
    stdout.write_text(
        "Name                          Stmts   Miss  Cover   Missing\n"
        "-----------------------------------------------------------\n"
        "src/awf/a.py                   100     10    90%   5-10\n"
        "-----------------------------------------------------------\n"
        "TOTAL                          100     10    90%\n",
        encoding="utf-8",
    )
    stderr = tmp_path / "stderr.txt"
    stderr.write_text(
        "Name                          Stmts   Miss  Cover   Missing\n"
        "-----------------------------------------------------------\n"
        "src/awf/b.py                   200     20    90%   15-25\n"
        "-----------------------------------------------------------\n"
        "TOTAL                          300     30    90%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([stdout, stderr])

    assert len(gaps) == 2
    files = {g["file"] for g in gaps}
    assert files == {"src/awf/a.py", "src/awf/b.py"}


@pytest.mark.unit
def test_parse_term_missing_handles_missing_column_with_no_lines(
    tmp_path: Path,
) -> None:
    coverage_output = tmp_path / "coverage.txt"
    coverage_output.write_text(
        "Name                          Stmts   Miss  Cover   Missing\n"
        "-----------------------------------------------------------\n"
        "src/awf/a.py                   100      0   100%\n"
        "src/awf/b.py                   200     10    95%   15\n"
        "-----------------------------------------------------------\n"
        "TOTAL                          300     10    97%\n",
        encoding="utf-8",
    )

    gaps = _parse_term_missing_gaps([coverage_output])

    assert len(gaps) == 2
    b_gap = next(g for g in gaps if g["file"] == "src/awf/b.py")
    assert b_gap["missing_lines"] == ["15"]
    a_gap = next(g for g in gaps if g["file"] == "src/awf/a.py")
    assert a_gap["missing_lines"] == []


@pytest.mark.unit
def test_missing_line_count_treats_malformed_tokens_as_single_gaps() -> None:
    assert _missing_line_count([object(), " 4 ", "10-12", "bad-range", "abc"]) == 6
