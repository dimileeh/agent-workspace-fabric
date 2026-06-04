# Review 4620252998 Monitor Event and GC Throttle Plan

## Problem Statement and Scope

The review-level comment identifies two outside-diff concerns in the completed
workspace monitor GC and service GC compose teardown paths:

- The unexpected completed-monitor filesystem GC exception path now logs
  `monitor.filesystem_gc_failed`, silently replacing the historical
  `monitor.filesystem_gc_raised` alert signal.
- `_run_gc_compose_teardowns` starts every candidate compose teardown at once,
  which can create an unbounded burst of Docker daemon requests during periodic
  service GC.

Scope is limited to restoring the historical exception log event and bounding
compose teardown fan-out while preserving per-candidate failure recording.

## Requirements Checklist

- Verify the review claims against the current code before editing.
- Restore `monitor.filesystem_gc_raised` for unexpected
  `run_workspace_filesystem_gc` exceptions while keeping normal partial-GC
  failures on `monitor.filesystem_gc_failed`.
- Preserve existing compose teardown outcome logging after a GC exception.
- Add a bounded compose teardown concurrency limit so large GC batches cannot
  issue one Docker teardown per candidate simultaneously.
- Preserve candidate-order result serialization and exception-to-result
  normalization.
- Add/update focused regression tests for the restored event name and bounded
  concurrency behavior.
- Run only focused local checks; AWF/GitHub own broad validation after the agent
  phase.

## Implementation Steps

1. Update the completed-monitor exception-path tests to expect
   `monitor.filesystem_gc_raised`.
2. Replace the unbounded-concurrency compose teardown test with a bounded
   concurrency regression that proves later candidates wait until a slot frees.
3. Run the focused tests to confirm they fail against the current code when
   practical.
4. Change lifecycle exception logging to emit
   `monitor.filesystem_gc_raised`.
5. Add a small compose teardown concurrency constant and gate
   `_run_gc_compose_teardowns` with an `asyncio.Semaphore`.
6. Re-run the focused tests, targeted lint for touched files, and `git diff
   --check`.
7. Record validation evidence in the paired validation document.

## Assumptions/Changes

- While searching for existing assertions, one additional focused edge test for
  the same swallowed completed-monitor GC exception path was found in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/`. It is in
  scope because leaving it on `monitor.filesystem_gc_failed` would create
  contradictory policy evidence for the restored event name.

## Verification Commands and Pass Criteria

- Failing-first/final focused tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_completed_filesystem_gc_exception_is_logged_and_swallowed tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_are_bounded -q`
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/service/gc.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/service/test_gc_parts/test_gc_part_004.py`
- Diff hygiene:
  `git diff --check`

Pass criteria: focused tests, targeted lint, and diff hygiene pass. Full
AWF/GitHub validation is intentionally left to AWF after agent completion.
