"""Tests for pytest-cov term-missing gap metadata extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.models import ProfileCoverage
from awf.runtime import validation_coverage as coverage_helpers
from awf.runtime.validation import (
    HEALTHCHECK_COMMAND_FAILED,
    HEALTHCHECK_INVALID_CONFIGURATION,
    ProfileHealthCheck,
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationRunner,
    _healthcheck_attempt_timeout,
    _healthcheck_cli_args,
    _healthcheck_failure_reason,
    _missing_line_count,
    _parse_python_coverage_percent_from_files,
    _parse_term_missing_gaps,
    _runs_pytest_under_coverage,
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
def test_python_coverage_parser_handles_deferred_fail_under_context(
    tmp_path: Path,
) -> None:
    delayed_fail_under = tmp_path / "delayed_fail_under.txt"
    delayed_fail_under.write_text(
        "FAIL Required test coverage of 99.0% not reached.\n"
        "coverage table footer\n"
        "Total coverage: 98.7%\n",
        encoding="utf-8",
    )
    stale_recent_total = tmp_path / "stale_recent_total.txt"
    stale_recent_total.write_text(
        "Total coverage: 97.1%\n"
        "unrelated line\n"
        "another unrelated line\n"
        "FAIL Required test coverage of 99.0% not reached.\n",
        encoding="utf-8",
    )
    combined_contexts = tmp_path / "combined_contexts.txt"
    combined_contexts.write_text(
        "FAIL Required test coverage of 99.0% not reached.\n"
        "Total coverage: 97.1%\n"
        "unrelated line\n"
        "FAIL Required test coverage of 99.0% not reached.\n"
        "coverage table footer\n"
        "Total coverage: 98.7%\n",
        encoding="utf-8",
    )

    assert _parse_python_coverage_percent_from_files([delayed_fail_under]) == 98.7
    assert _parse_python_coverage_percent_from_files([stale_recent_total]) == 97.1
    assert _parse_python_coverage_percent_from_files([combined_contexts]) == 98.7


@pytest.mark.unit
def test_pytest_under_coverage_scan_rejects_plain_coverage_report() -> None:
    assert not _runs_pytest_under_coverage(["coverage", "report"])


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
def test_healthcheck_helpers_cover_invalid_configuration_edges() -> None:
    healthcheck = ProfileHealthCheck.model_construct(
        name="broken",
        kind=None,
        command=None,
        url=None,
        method="GET",
        expected_status=200,
        timeout_seconds=30.0,
        interval_seconds=1.0,
        attempt_timeout_seconds=None,
    )
    latest = ValidationCommandResult(
        command="invalid healthcheck",
        returncode=2,
        duration_seconds=0.1,
        stdout_path=Path("/tmp/stdout"),
        stderr_path=Path("/tmp/stderr"),
    )

    assert _healthcheck_cli_args(healthcheck) == [
        "python",
        "-c",
        "import sys; print('invalid healthcheck configuration', file=sys.stderr); sys.exit(2)",
    ]
    assert _healthcheck_attempt_timeout(healthcheck, 0) == 0.001
    assert _healthcheck_failure_reason(healthcheck, latest) == HEALTHCHECK_INVALID_CONFIGURATION

    command_healthcheck = ProfileHealthCheck.model_validate(
        {"name": "cmd", "command": "curl -f http://api/health"}
    )
    assert _healthcheck_failure_reason(command_healthcheck, latest) == HEALTHCHECK_COMMAND_FAILED


@pytest.mark.unit
async def test_healthcheck_stderr_append_skips_invalid_log_stream_id(tmp_path: Path) -> None:
    class _LogStore:
        def __init__(self) -> None:
            self.append_calls = 0

        async def append_to_stream(self, **_kwargs: object) -> None:
            self.append_calls += 1

    stderr_path = tmp_path / "health.stderr"
    stderr_path.write_text("before", encoding="utf-8")
    result = ValidationCommandResult(
        command="curl -f http://api/health",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=tmp_path / "health.stdout",
        stderr_path=stderr_path,
        phase="healthcheck",
        stream_ids={"stderr": "validation.01_healthcheck.stdout"},
    )
    log_store = _LogStore()
    runner = ValidationRunner(
        runner=object(),  # type: ignore[arg-type]
        artifacts_dir=tmp_path,
        log_store=log_store,  # type: ignore[arg-type]
    )

    await runner._append_healthcheck_stderr(  # noqa: SLF001
        workspace_id="ws_health",
        result=result,
        diagnostic="\nhealthcheck failed\n",
    )

    assert stderr_path.read_text(encoding="utf-8").endswith("\nhealthcheck failed\n")
    assert log_store.append_calls == 0


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


@pytest.mark.unit
def test_coverage_command_plan_handles_no_command_and_non_pytest_commands() -> None:
    assert coverage_helpers.coverage_command_plan(ProfileCoverage()).command == ""

    plan = coverage_helpers.coverage_command_plan(
        ProfileCoverage(command="coverage report", parallel_workers=5),
        parallel_worker_cpu_limit=2,
    )

    assert plan.command == "coverage report"
    assert plan.parallel_workers_requested == 5
    assert plan.parallel_workers_effective is None
    assert plan.parallel_distribution is None


@pytest.mark.unit
def test_inject_pytest_parallel_workers_keeps_unparseable_or_non_pytest_commands() -> None:
    assert (
        coverage_helpers._inject_pytest_parallel_workers(
            "pytest 'unterminated", workers=3, distribution="loadscope"
        )
        == "pytest 'unterminated"
    )
    assert (
        coverage_helpers._inject_pytest_parallel_workers(
            "python -m unittest",
            workers=3,
            distribution="loadscope",
        )
        == "python -m unittest"
    )
    assert (
        coverage_helpers._inject_pytest_parallel_workers(
            "coverage report",
            workers=3,
            distribution="loadscope",
        )
        == "coverage report"
    )


@pytest.mark.unit
def test_inject_pytest_parallel_workers_keeps_command_when_parse_or_pytest_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pytest worker injection preserves commands when parsing cannot proceed."""
    monkeypatch.setattr(coverage_helpers, "_is_pytest_coverage_command", lambda _command: True)

    assert (
        coverage_helpers._inject_pytest_parallel_workers(
            "pytest --cov=awf 'unterminated",
            workers=3,
            distribution="loadscope",
        )
        == "pytest --cov=awf 'unterminated"
    )
    assert (
        coverage_helpers._inject_pytest_parallel_workers(
            "coverage run -m unittest --cov=awf",
            workers=3,
            distribution="loadscope",
        )
        == "coverage run -m unittest --cov=awf"
    )


@pytest.mark.unit
def test_read_text_if_present_handles_missing_paths(tmp_path: Path) -> None:
    assert coverage_helpers._read_text_if_present(tmp_path / "missing.txt") is None
