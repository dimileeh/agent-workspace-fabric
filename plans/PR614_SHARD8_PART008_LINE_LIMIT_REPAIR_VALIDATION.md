# PR614 Shard 8 Part 008 Line Limit Repair Validation

Plan reference: `plans/PR614_SHARD8_PART008_LINE_LIMIT_REPAIR_PLAN.md`

## Requirement Status

- Keep the current AWF-managed branch and do not push: Complete.
- Do not edit workflow, quality-gate, or protected configuration files:
  Complete.
- Move complete tests out of the oversized file without weakening assertions:
  Complete.
- Keep all affected first-party code files under the line limit: Complete.
- Run focused validation only: Complete.
- Record that full AWF/GitHub validation remains owned by AWF after
  completion: Complete.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_033.py`
- `plans/PR614_SHARD8_PART008_LINE_LIMIT_REPAIR_PLAN.md`
- `plans/PR614_SHARD8_PART008_LINE_LIMIT_REPAIR_VALIDATION.md`

Focused checks run:

- `wc -l tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_033.py`
  reported 1429 lines for part 008 and 133 lines for part 033.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_033.py -q`
  passed: 26 passed.

Full AWF/GitHub sharded coverage and broad CI validation were not run locally;
AWF owns those gates after agent completion.
