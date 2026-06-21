# PR608 Line Limit Shard 8 CI Fix Validation

Plan reference: `plans/PR608_LINE_LIMIT_SHARD8_CI_FIX_PLAN.md`

## Requirement Status

- Complete: Kept the current AWF-managed branch and did not push or rebase.
- Complete: Did not edit protected workflow or quality-gate configuration files.
- Complete: Reduced
  `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`
  from 1505 lines to 1245 lines by moving behavioral tests into
  `test_executor_coverage_edges_part_013.py`.
- Complete: Preserved the moved post-validation conformance cleanup tests in
  the new shard without deleting, skipping, or weakening assertions.
- Complete: Ran focused verification only.
- Complete: Broad AWF/GitHub validation, coverage gates, and CI-equivalent
  suites were not run locally; AWF owns those after agent completion.

## Evidence

Files changed:

- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_013.py`
- `plans/PR608_LINE_LIMIT_SHARD8_CI_FIX_PLAN.md`
- `plans/PR608_LINE_LIMIT_SHARD8_CI_FIX_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Initial repro failed with
    `test_executor_coverage_edges_part_002.py: 1505`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_013.py -q`
  - Passed: `4 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_013.py`
  - Passed after import cleanup.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_013.py -q`
  - Passed: `24 passed`.

CI log observations:

- `python-coverage-shards (8)` failed on the line-limit guard for
  `test_executor_coverage_edges_part_002.py`.
- `lint-and-type` failed during `uv pip install -e ".[dev]"` while fetching
  PyPI metadata for `jaraco_context-6.1.2` with a broken-pipe connection error,
  before lint or type checks executed. No code diagnostic was present in that
  job log.
