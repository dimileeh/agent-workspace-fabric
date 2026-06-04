# Review 4620252998 Monitor Event and GC Throttle Validation

Plan reference: `plans/REVIEW_4620252998_MONITOR_EVENT_AND_GC_THROTTLE_PLAN.md`

## Requirement Status

- Verify the review claims against the current code before editing: Complete.
  Current lifecycle code logged `monitor.filesystem_gc_failed` in the
  unexpected exception handler, and `_run_gc_compose_teardowns` used unbounded
  `asyncio.gather`.
- Restore `monitor.filesystem_gc_raised` for unexpected
  `run_workspace_filesystem_gc` exceptions while keeping normal partial-GC
  failures on `monitor.filesystem_gc_failed`: Complete.
- Preserve existing compose teardown outcome logging after a GC exception:
  Complete.
- Add a bounded compose teardown concurrency limit so large GC batches cannot
  issue one Docker teardown per candidate simultaneously: Complete.
- Preserve candidate-order result serialization and exception-to-result
  normalization: Complete.
- Add/update focused regression tests for the restored event name and bounded
  concurrency behavior: Complete.
- Run only focused local checks; AWF/GitHub own broad validation after the
  agent phase: Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/lifecycle.py`
- `src/awf/service/gc.py`
- `tests/unit/runtime/test_monitor_completion_gc.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
- `tests/unit/service/test_gc_parts/test_gc_part_004.py`
- `plans/REVIEW_4620252998_MONITOR_EVENT_AND_GC_THROTTLE_PLAN.md`
- `plans/REVIEW_4620252998_MONITOR_EVENT_AND_GC_THROTTLE_VALIDATION.md`

Focused checks:

- Failing-first check before production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_are_bounded -q`
  failed with `4 failed`: the monitor exception path still emitted
  `monitor.filesystem_gc_failed`, and the GC module had no compose teardown
  concurrency limit.
- Passing focused behavior check after production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_logs_compose_teardown_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_completed_filesystem_gc_exception_is_logged_and_swallowed tests/unit/service/test_gc_parts/test_gc_part_004.py::test_gc_compose_teardowns_are_bounded -q`
  passed with `5 passed`.
- Adjacent failure-normalization/side-effect regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_004.py::test_batch_terminal_gc_compose_teardown_failure_blocks_runtime_side_effects -q`
  passed with `1 passed`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/service/gc.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/service/test_gc_parts/test_gc_part_004.py`
  passed.
- Diff hygiene:
  `git diff --check`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run locally.

## Gaps

None.
