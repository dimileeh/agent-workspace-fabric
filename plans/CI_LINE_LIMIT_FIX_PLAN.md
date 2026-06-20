# CI Line Limit Fix Plan

## Problem Statement and Scope

PR #614 is failing the GitHub Actions `python-coverage-shards (8)` check because
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
has 1504 lines, exceeding the repository maintainability limit of 1500 lines.

Scope is limited to preserving the existing PR monitor recovery tests while
moving enough test content out of the oversized part file to satisfy the guard.

## Requirements Checklist

- Keep all existing test behavior and assertions intact.
- Do not weaken or skip the maintainability check.
- Keep every touched first-party file under the 1500-line limit.
- Run focused validation only, leaving broad AWF/GitHub validation to AWF after
  agent completion.

## Implementation Steps

1. Move the final `_commit_dirty_worktree` unrecoverable-head regression test
   from `test_pr_monitor_runner_coverage_edges_part_019.py` into the adjacent
   part file, `test_pr_monitor_runner_coverage_edges_part_020.py`.
2. Add only the import needed by the moved test in part 020.
3. Verify file line counts and run the focused maintainability test.
4. Run the moved runtime test directly to prove behavior was preserved.

## Verification Commands and Pass Criteria

- `wc -l tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  - Both files must be at or below 1500 lines.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_commit_dirty_worktree_fails_closed_on_unrecoverable_head -q`
  - Must pass.

Full AWF/GitHub validation remains managed by AWF after agent completion.
