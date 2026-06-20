# PRRT_kwDOSJAM6s6KySdC Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6KySdC_PLAN.md`

## Requirement Status

- Complete: Verify a parsed per-item `HEAD` has an existing commit object before
  using it. `fix_cycle.py` now runs `git cat-file -e <head>^{commit}` before
  returning a per-item current head.
- Complete: Fall back to the cycle-opening `operation_start_head` when the
  current `HEAD` ref is missing or points at a missing commit object. The helper
  keeps the previous missing-ref fallback and adds a missing-object fallback.
- Complete: Add a focused regression test for a poisoned per-item `HEAD`.
  `test_fix_cycle_falls_back_when_per_item_head_object_is_poisoned` covers a
  two-item fix cycle where the second parsed `HEAD` is unresolvable.
- Complete: Keep changes scoped to the fix-cycle behavior and its tests.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py -q -k poisoned`
  - First run before the production fix failed with `['start', 'poisoned']` vs
    expected `['start', 'start']`.
  - Final run passed: `1 passed, 22 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py -q -k "head_object_missing or poisoned"`
  passed: `3 passed, 20 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py`
  passed.

Full AWF/GitHub validation was not run locally per the workspace contract; AWF
will manage broad validation after agent completion.
