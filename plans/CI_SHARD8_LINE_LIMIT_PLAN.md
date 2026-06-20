# CI Shard 8 Line Limit Plan

## Problem Statement and Scope

GitHub Actions CI for PR #614 fails in `python-coverage-shards (8)` because
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
reports one oversized first-party test module:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

The fix is limited to splitting that oversized test module into smaller adjacent
part files without changing production code or weakening the maintainability
guard.

After the initial log inspection, `python-coverage-shards (6)` also completed
with a behavioral failure in
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py::test_execute_sync_base_protected_violation_pauses_into_blocked_not_terminal`.
That failure is also in scope for this CI fix cycle.

## Requirements Checklist

- [x] Keep all files under the repo's first-party line limit.
- [x] Preserve the behavior covered by the moved tests.
- [x] Do not edit workflow, quality-gate, or protected configuration files.
- [x] Preserve the expected SyncBase protected-scope pause behavior: protected
      violations during base-conflict resolution leave the workspace blocked,
      not failed.
- [x] Run focused validation only; AWF/GitHub own broad validation after agent
      completion.
- [x] Commit the local fix on the current AWF branch without pushing.

## Implementation Steps

1. Identify whole test function boundaries in the oversized module.
2. Move a contiguous set of tests into a new adjacent part module, copying only
   imports and helper functions needed by those moved tests.
3. Keep shared helpers in the original module if remaining tests still use them.
4. Run focused maintainability and affected test-file checks.
5. Reproduce the SyncBase protected-scope pause failure with the single failing
   test.
6. Patch the smallest production or fixture gap causing the workspace to end
   failed instead of blocked.
7. Write validation notes and commit the plan, validation, and fixes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_022.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py::test_execute_sync_base_protected_violation_pauses_into_blocked_not_terminal -q`
  passes.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
