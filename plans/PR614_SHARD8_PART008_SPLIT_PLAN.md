# PR614 Shard 8 Part 008 Split Plan

## Problem Statement And Scope

PR #614 current-head CI fails in `python-coverage-shards (8)` because
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
has 1554 lines and violates the first-party file line limit of 1500 lines.

Scope is limited to splitting that oversized test file. Production behavior,
test assertions, CI workflow configuration, and quality gates are out of scope.

## Requirements Checklist

- Confirm the line-limit guard fails locally with the focused maintainability test.
- Move enough tests from part 008 into a new adjacent part file so every
  first-party file is under 1500 lines.
- Preserve the moved tests' behavior and assertions.
- Run focused verification for the moved tests and the line-limit guard.
- Do not run full AWF/GitHub-owned broad validation locally.

## Implementation Steps

1. Run the focused line-limit guard to reproduce the failure.
2. Move the final protected-scope repair edge tests from part 008 into a new
   `test_pr_monitor_runner_coverage_edges_part_029.py` file.
3. Keep only the imports and fixture needed by the new file.
4. Re-run the focused line-limit guard and the new part file tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Before the split: fails with part 008 over 1500 lines.
  - After the split: passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py -q`
  - Passes with the moved tests unchanged in behavior.

Full AWF/GitHub validation remains managed by AWF after agent completion.
