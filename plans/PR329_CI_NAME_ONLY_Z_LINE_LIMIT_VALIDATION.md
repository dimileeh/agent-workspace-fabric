# PR329 CI Name-Only Z Line Limit Validation

Plan reference: `PR329_CI_NAME_ONLY_Z_LINE_LIMIT_PLAN.md`

## Requirement Status

- Preserve valid NUL-delimited `--name-only -z` parsing and de-duplication:
  Complete.
- Reject newline-delimited output that is not NUL-delimited: Complete.
- Reject truncated NUL output missing the final terminator: Complete.
- Reject empty path records: Complete.
- Bring `helpers.py` to 1,500 lines or fewer without changing public helper
  compatibility names: Complete.
- Run only focused local checks: Complete. Full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `plans/PR329_CI_NAME_ONLY_Z_LINE_LIMIT_PLAN.md`
- `plans/PR329_CI_NAME_ONLY_Z_LINE_LIMIT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest 'tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_changed_paths_from_name_only_z_rejects_malformed_z_output[src/fix.py\n-expected NUL-delimited output]' 'tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_changed_paths_from_name_only_z_rejects_malformed_z_output[src/fix.py\x00tests/test_fix.py-missing terminating NUL]' tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Initial result before fix: failed with the reported malformed parser and
    line-limit failures.
  - Final result after fix: `3 passed in 0.83s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_changed_paths_from_name_only_z_deduplicates_valid_nul_records tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_changed_paths_from_name_only_z_rejects_malformed_z_output -q`
  - Result: `4 passed in 0.75s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/path_helpers.py src/awf/runtime/pr_monitor_runner/helpers.py`
  - Result: `All checks passed!`.
- `wc -l src/awf/runtime/pr_monitor_runner/helpers.py`
  - Result: `1498 src/awf/runtime/pr_monitor_runner/helpers.py`.

## Gaps

No planned requirements are partial or missing.
