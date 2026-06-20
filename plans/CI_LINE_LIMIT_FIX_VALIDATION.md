# CI Line Limit Fix Validation

Plan reference: `plans/CI_LINE_LIMIT_FIX_PLAN.md`

## Requirement Status

- Complete: Keep all existing test behavior and assertions intact.
  - The `_commit_dirty_worktree` unrecoverable-head regression test was moved
    from part 019 to part 020 without changing its assertions.
- Complete: Do not weaken or skip the maintainability check.
  - No quality gate code or configuration was changed.
- Complete: Keep every touched first-party file under the 1500-line limit.
  - `test_pr_monitor_runner_coverage_edges_part_019.py`: 1440 lines.
  - `test_pr_monitor_runner_coverage_edges_part_020.py`: 1466 lines.
- Complete: Run focused validation only.
  - Focused checks were run locally. Full AWF/GitHub validation is managed by
    AWF after agent completion.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
- `plans/CI_LINE_LIMIT_FIX_PLAN.md`
- `plans/CI_LINE_LIMIT_FIX_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Initial focused repro failed with `part_019.py` at 1504 lines.
- `wc -l tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  - Passed the planned line-count criteria: 1440 and 1466 lines.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: 1 test passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_commit_dirty_worktree_fails_closed_on_unrecoverable_head -q`
  - Passed: 1 test passed.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  - Passed.

## Gaps

None for the scoped fix. Broad CI and coverage-gate validation were not run
locally per the AWF workspace contract.
