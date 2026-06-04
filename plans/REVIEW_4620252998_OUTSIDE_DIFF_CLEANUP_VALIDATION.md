# Review 4620252998 Outside-Diff Cleanup Validation

Plan reference: `plans/REVIEW_4620252998_OUTSIDE_DIFF_CLEANUP_PLAN.md`

## Requirement Status

- Complete: Verified the current code had the `monitor.filesystem_gc_raised`
  exception event, an adjacent `FAILED_NO_WORK_TERMINAL_STATUSES` import block,
  and sequential `_run_gc_compose_teardowns` candidate iteration.
- Complete: Unexpected completed-monitor filesystem GC exceptions now emit the
  historical `monitor.filesystem_gc_failed` event while retaining compose
  teardown outcome logging.
- Complete: The duplicate `gc_classify` import block was removed by reading
  `FAILED_NO_WORK_TERMINAL_STATUSES` through the module alias.
- Complete: Candidate compose teardowns now start concurrently via
  `asyncio.gather`, while ordinary `Exception` failures still become structured
  failed teardown results.
- Complete: Focused tests cover event compatibility and the concurrent
  candidate-start behavior.
- Complete: Only focused tests and targeted lint were run locally; AWF/GitHub
  own broad validation after agent completion.

## Evidence

Initial focused regression check before implementation:

`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_completed_filesystem_gc_exception_is_logged_and_swallowed tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_start_later_candidates_while_first_is_pending -q`

Failed with 4 failures: the monitor tests still saw
`monitor.filesystem_gc_raised`, and the concurrency test timed out waiting for
the second teardown to start.

Final focused regression check:

`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_completed_filesystem_gc_exception_is_logged_and_swallowed tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_start_later_candidates_while_first_is_pending -q`

Passed: 4 tests.

Adjacent teardown failure checks:

`uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_004.py::test_batch_terminal_gc_compose_teardown_failure_blocks_runtime_side_effects tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_records_raised_missing_workspace_compose_teardown -q`

Passed: 2 tests.

Targeted lint:

`uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/service/test_gc_parts/test_gc_part_004.py`

Passed.

## Gaps

None for the scoped review-level comment. Full repository validation, coverage,
push, PR update, and merge gating remain owned by AWF/GitHub after this agent
phase.
