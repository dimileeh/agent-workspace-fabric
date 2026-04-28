# Plan: Coverage Gap Metadata from pytest-cov term-missing Output

## Objective

When AWF's coverage command produces a pytest-cov `term-missing` table in its
output, extract the most actionable missing files/line ranges into structured
coverage metadata and operator-facing failure/fix context. This makes coverage
failures explain *what* is uncovered rather than only reporting a threshold
percentage. Preserve all existing behavior for COVERAGE_NOT_FOUND, unsupported
providers, baseline-debt no-regression, command failures, and enforce/reported
status routing. Do not lower any threshold, profile requirement, or PRD quality
gate.

## Current Code Context

- **Coverage command execution & parsing**: `src/awf/runtime/validation.py`
  - `_COVERAGE_TOTAL_RE` (line 45) and `_COVERAGE_SUMMARY_RE` (line 46) extract a percentage from stdout/stderr.
  - `_parse_python_coverage_percent_from_files()` (line 577) reads output files, finds TOTAL / summary lines, returns float or None. It does **not** parse the term-missing table.
  - `ValidationCoverageResult.as_metadata()` (line 87) produces the metadata dict stored in `log_stream_refs`.
  - `_coverage_reason_code()` (line 594) classifies: COVERAGE_OK / COVERAGE_BELOW_THRESHOLD / COVERAGE_NOT_FOUND / COVERAGE_COMMAND_FAILED.

- **Coverage stored on validation_runs**: `src/awf/control/executor.py`
  - `_validation_run_coverage_metadata()` (line 2407) builds the metadata dict with optional baseline fields.
  - `_finish_validation_run()` (line 2082) stores coverage dict in `ValidationRun.log_stream_refs.coverage` JSON blob.
  - `_apply_baseline_coverage_ratchet()` (line 2342) adjusts COVERAGE_BELOW_THRESHOLD → COVERAGE_BASELINE_DEBT_NO_REGRESSION when baseline debt is not regressed.
  - `_validation_failure_message()` (line 2426) produces the human-readable message for COVERAGE_BELOW_THRESHOLD — currently says "coverage X% is below required Y%; add meaningful tests" but lists **no specific gaps**.

- **API exposure**: `src/awf/api/validation_runs.py`
  - `validation_coverage_fields()` (line 86) extracts coverage_percent, coverage_minimum_percent, coverage_status, coverage_reason_code from `log_stream_refs`. No gap fields.
  - `validation_run_summary()` (line 17) builds `ValidationRunSummaryResponse`.

- **API schemas**: `src/awf/api/schemas.py`
  - `ValidationRunSummaryResponse` (line 453) has coverage_percent / coverage_minimum_percent / coverage_status / coverage_reason_code.
  - `ValidationProvenanceItemResponse` (line 635) has the same four coverage fields.

- **Profile model**: `src/awf/profiles/models.py`
  - `ProfileCoverage` (line 105): minimum_percent, enforce, provider, command.

- **Database model**: `src/awf/db/models.py`
  - `ValidationRun.log_stream_refs` (line 632): JSON column that currently stores `{"commands": [...], "coverage": {...}}`.

- **Quality gate protection**: `src/awf/control/quality_gates.py`
  - Protects `.awf/workspace.yml`, `.coveragerc`, `pyproject.toml`, etc. from unauthorized edits.

## Intended Files And Modules To Touch

### Production code

- **`src/awf/runtime/validation.py`**
  - Add a new function `_parse_term_missing_gaps()` that parses the pytest-cov term-missing table from output files.
  - Add `gaps` field to `ValidationCoverageResult` dataclass (list of small structured dicts with file + missing line ranges).
  - Enhance `as_metadata()` to include `gaps` in the output dict.
  - Wire the gap parsing into `_collect_coverage()` so gaps are attached to the result.

- **`src/awf/control/executor.py`**
  - Enhance `_validation_failure_message()` to include a short top-gap list when coverage is below threshold.
  - Enhance `_validation_run_coverage_metadata()` to forward gaps into the stored metadata.
  - Update the `ValidationFixContext` construction and `build_fix_prompt()` call path so fix prompts also see gap info (this may be a simple data-class field addition in `src/awf/control/validation_fix_cycle.py`).

- **`src/awf/control/validation_fix_cycle.py`**
  - Add `coverage_gaps` field to `ValidationFixContext` dataclass.
  - Update `build_fix_prompt()` to include gap info when present.

- **`src/awf/api/schemas.py`**
  - Add `coverage_gaps` field to `ValidationRunSummaryResponse` and `ValidationProvenanceItemResponse`.

- **`src/awf/api/validation_runs.py`**
  - Update `validation_coverage_fields()` to extract `coverage_gaps` from `log_stream_refs`.

### Tests

- **`tests/unit/runtime/test_validation_coverage_gaps.py`** (new file)
  - Test `_parse_term_missing_gaps()` with realistic pytest-cov output.
  - Test edge cases: empty term-missing table, only TOTAL line, corrupted output, no output, mixed stdout/stderr.
  - Test `ValidationCoverageResult.as_metadata()` includes gaps.
  - Test `_collect_coverage()` produces gap metadata end to end (with a mocked file system).
  - Test `ValidationCoverageResult.ok` is unchanged by gap presence.

- **`tests/unit/control/test_executor_coverage_gaps.py`** (new file)
  - Test `_validation_failure_message()` includes top-gap list when gaps exist.
  - Test `_validation_failure_message()` preserves existing behavior when gaps are empty/None.
  - Test `_validation_run_coverage_metadata()` includes gap fields.
  - Test baseline-debt ratchet preserves gaps in metadata (gaps should still be reported even when status flips to baseline_debt).

- **`tests/unit/api/test_validation_coverage_gaps.py`** (new file)
  - Test `validation_coverage_fields()` extracts gaps from `log_stream_refs`.
  - Test `validation_run_summary()` includes gaps field.
  - Test schema round-trips: `coverage_gaps` field in `ValidationRunSummaryResponse` and `ValidationProvenanceItemResponse`.

## Tests To Write First (TDD)

1. **`test_parse_term_missing_extracts_gaps_from_stdout`** — `tests/unit/runtime/test_validation_coverage_gaps.py`
   - Setup: write a realistic pytest-cov term-missing table to a temp file (including the header line `Name / Stmts / Miss / Cover / Missing`).
   - Assertions: returns a list of gap dicts with correct file paths and missing line ranges.

2. **`test_parse_term_missing_handles_empty_output`** — `tests/unit/runtime/test_validation_coverage_gaps.py`
   - Setup: temp file with only the TOTAL line, no term-missing table.
   - Assertions: returns empty list.

3. **`test_parse_term_missing_handles_no_output`** — `tests/unit/runtime/test_validation_coverage_gaps.py`
   - Setup: empty file or non-existent path.
   - Assertions: returns empty list (no crash).

4. **`test_coverage_result_metadata_includes_gaps`** — `tests/unit/runtime/test_validation_coverage_gaps.py`
   - Setup: construct a `ValidationCoverageResult` with gaps populated.
   - Assertions: `as_metadata()` dict contains `gaps` key with correct list.

5. **`test_failure_message_includes_top_gaps`** — `tests/unit/control/test_executor_coverage_gaps.py`
   - Setup: construct a `ValidationResult` with coverage below threshold and gaps populated.
   - Assertions: `_validation_failure_message()` includes "top uncovered" gap list or file references.

6. **`test_failure_message_preserves_behavior_without_gaps`** — `tests/unit/control/test_executor_coverage_gaps.py`
   - Setup: `ValidationResult` with COVERAGE_BELOW_THRESHOLD but no gaps.
   - Assertions: output matches existing behavior exactly (no gap text injected).

7. **`test_validation_coverage_fields_extracts_gaps`** — `tests/unit/api/test_validation_coverage_gaps.py`
   - Setup: `ValidationRun` with `log_stream_refs` containing `coverage.gaps`.
   - Assertions: `validation_coverage_fields()` returns `coverage_gaps` with correct data.

8. **`test_baseline_debt_ratchet_preserves_gaps_in_metadata`** — `tests/unit/control/test_executor_coverage_gaps.py`
   - Setup: baseline coverage below threshold, result coverage below threshold with gaps. Apply ratchet.
   - Assertions: metadata still includes gaps; status changed to baseline_debt but gap data intact.

## Implementation Steps

1. **Add `_parse_term_missing_gaps()` to `src/awf/runtime/validation.py`**
   - Parse lines between the term-missing table header (`Name ... Stmts ... Miss ... Cover ... Missing`) and the TOTAL line.
   - Each data line: extract file path and the `Missing` column (comma-separated line numbers or ranges like `5-10`).
   - Store as list of `{"file": str, "missing_lines": list[str]}` dicts.
   - Limit to top N most actionable (the ones with the most missing lines, capped at 10).
   - Handle edge cases: truncated output, no table header, malformed lines.

2. **Add `gaps` field to `ValidationCoverageResult`**
   - `gaps: list[dict[str, object]] = field(default_factory=list)`
   - Update `as_metadata()` to include `"gaps": self.gaps` when non-empty.
   - Update all call sites that construct `ValidationCoverageResult` to pass gaps (default empty list).

3. **Wire gap parsing into `_collect_coverage()` in `validation.py`**
   - After calling `_parse_python_coverage_percent_from_files()` and before building the reason_code, also call `_parse_term_missing_gaps()` on the same output files.
   - Pass the result to `ValidationCoverageResult(gaps=...)`.

4. **Enhance `_validation_failure_message()` in `executor.py`**
   - For `COVERAGE_BELOW_THRESHOLD`: if gaps are non-empty, append a "top uncovered areas" list.
   - Format: `coverage X% is below required Y% (top 5 gaps: a/b/c.py:10-15, d/e.py:20-30, ...)`
   - Keep existing exact behavior when gaps are empty.

5. **Enhance `_validation_run_coverage_metadata()` in `executor.py`**
   - Forward gaps from `result.coverage.gaps` into the returned metadata dict.

6. **Add `coverage_gaps` to API schemas in `schemas.py`**
   - `ValidationRunSummaryResponse`: add `coverage_gaps: list[dict[str, object]] = Field(default_factory=list)`
   - `ValidationProvenanceItemResponse`: add same field.

7. **Update `validation_coverage_fields()` in `api/validation_runs.py`**
   - Extract `gaps` from the coverage sub-dict in `log_stream_refs`.
   - Return as `coverage_gaps`.

8. **Optional: Update `ValidationFixContext` and `build_fix_prompt()`**
   - Add `coverage_gaps` field to `ValidationFixContext`.
   - Include gap info in the fix prompt so the agent knows which files/lines need coverage.

## Validation Commands

Focused:
```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_coverage_gaps.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_gaps.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_coverage_gaps.py -q
```

Full surface:
```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks And Assumptions

- **Assumption**: pytest-cov `term-missing` output format (`Name / Stmts / Miss / Cover / Missing` header) is stable across pytest-cov versions >= 4.x.
- **Assumption**: Coverage output files fit in memory (they are per-workspace and bounded).
- **Risk**: If pytest-cov changes output format, gap parsing will silently return empty gaps — existing behavior is preserved (only threshold-based failure fires). Add a test with the current known format as a regression.
- **Risk**: The `gaps` field grows `log_stream_refs` JSON size. We cap at top 10 most-missing files, which adds at most a few KB.
- **Risk**: The `coverage_gaps` field added to response schemas changes the API contract. Since we add (not remove) fields and use `default_factory=list`, this is backward compatible.

## Explicit Non-Goals

- Do not modify database schema — gaps are stored in existing `log_stream_refs` JSON column (same as current coverage metadata).
- Do not add console UI for gaps (no frontend changes unless existing schemas auto-expose them — console consumption is a follow-up slice).
- Do not change coverage threshold / fail_under / `.awf/workspace.yml` coverage config / pyproject.toml.
- Do not change baseline-debt no-regression logic (`_apply_baseline_coverage_ratchet` remains untouched; only surrounding metadata is enhanced).
- Do not add support for non-python coverage providers — gap parsing is Python/pytest-cov only.
- Do not make the gap list exhaustive — top-10 cap is intentional for actionable signal.
- Do not add retry logic or infrastructure recovery — this is a pure parsing/metadata/messaging feature.
- Do not switch branches, push, or open a PR.
