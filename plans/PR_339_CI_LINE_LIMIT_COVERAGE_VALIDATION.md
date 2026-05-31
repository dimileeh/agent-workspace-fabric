# PR #339 CI Line Limit and Coverage Validation

Plan reference: `plans/PR_339_CI_LINE_LIMIT_COVERAGE_PLAN.md`

## Requirement Status

- Complete: Keep the AWF-managed branch and avoid push/rebase/protected config
  edits.
  - Evidence: Work stayed on the current branch; no workflow, quality-gate, or
    configuration files were edited.
- Complete: Reduce oversized first-party files below the line limit.
  - Evidence: `wc -l` reports:
    - `src/awf/runtime/pr_monitor_runner/helpers.py`: 1411 lines.
    - `src/awf/runtime/pr_monitor_runner/path_parsing.py`: 189 lines.
    - `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`:
      1487 lines.
    - `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py`:
      311 lines.
- Complete: Preserve behavior while decomposing helper code.
  - Evidence: Git porcelain and diff parsing helpers were extracted to
    `src/awf/runtime/pr_monitor_runner/path_parsing.py` and explicitly
    re-exported from `helpers.py` for existing callers.
- Complete: Add focused coverage for uncovered PR monitor runner branches.
  - Evidence:
    `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py`
    adds path parsing coverage for quoted rename splitting, C-style escape
    decoding, malformed `--porcelain -z` records, trailing-backslash decoding,
    and `--name-only -z` error handling.
- Complete: Run focused local validation only.
  - Evidence commands:
    - `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
      passed: `1 passed`.
    - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py -q`
      passed: `25 passed`.
    - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py -q`
      passed after the final targeted path parser assertion update: `5 passed`.
    - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/path_parsing.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py`
      passed.
    - `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/path_parsing.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_009.py`
      passed.
    - `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/path_parsing.py`
      passed.

## Remaining Gaps

None for the saved plan. Full AWF/GitHub validation, including the repository
wide coverage gate, was intentionally not run locally per the workspace
contract; AWF owns that broad validation after agent completion.
