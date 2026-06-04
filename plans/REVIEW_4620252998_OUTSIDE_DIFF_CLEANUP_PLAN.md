# Review 4620252998 Outside-Diff Cleanup Plan

## Problem Statement and Scope

The review-level PR comment calls out three remaining GC/monitor cleanup issues:

- Unexpected completed-monitor filesystem GC exceptions now emit
  `monitor.filesystem_gc_raised` instead of the historical
  `monitor.filesystem_gc_failed`, which can break existing operator log queries.
- `src/awf/service/gc.py` has adjacent imports from `awf.service.gc_classify`
  that can be merged.
- `_run_gc_compose_teardowns` processes terminal GC candidates sequentially,
  so one slow teardown delays starting later candidate teardowns.

Scope is limited to those three points, with focused tests for behavior changes.

## Requirements Checklist

- Verify each review claim against current code before editing.
- Preserve the legacy `monitor.filesystem_gc_failed` event for unexpected GC
  exception paths while keeping compose teardown outcome logging intact.
- Merge the duplicate `gc_classify` import block without changing exported API.
- Start candidate compose teardowns concurrently and keep exception-to-result
  normalization for ordinary `Exception` failures.
- Add or update focused tests for event compatibility and teardown concurrency.
- Run only focused local checks; AWF/GitHub own broad validation after the agent
  phase.

## Implementation Steps

1. Update focused tests so completed-monitor GC exception cases expect
   `monitor.filesystem_gc_failed`, and add a service GC regression proving later
   teardown candidates are started while an earlier candidate is still pending.
2. Run the focused tests to confirm the current implementation fails when
   practical.
3. Update lifecycle logging to use the historical failure event for unexpected
   GC exceptions.
4. Merge the adjacent `gc_classify` import blocks in `src/awf/service/gc.py`.
5. Update `_run_gc_compose_teardowns` to fan out candidate teardown calls and
   collect results in candidate order.
6. Re-run the focused tests and targeted lint for touched files, then record
   validation evidence.

## Verification Commands and Pass Criteria

- Initial/final focused tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_completed_filesystem_gc_exception_is_logged_and_swallowed tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_start_later_candidates_while_first_is_pending -q`
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/service/test_gc_parts/test_gc_part_004.py`

Pass criteria: focused tests and targeted lint pass. Full AWF/GitHub validation
is intentionally not run in the agent phase.
