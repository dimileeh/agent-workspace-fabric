# CI PR396 Monitor Completion Teardown Validation

Plan reference: `plans/CI_PR396_MONITOR_COMPLETION_TEARDOWN_PLAN.md`

## Requirement Status

- Reproduce the reported focused failures before edits: Complete.
  The provided integration repro failed with five compose teardown assertion
  failures; the reported merge-coordinator unit repro failed because no
  fake-runner `docker compose` call was observed.
- Keep completed-workspace cleanup routed through filesystem GC and the
  completed-workspace compose teardown callback: Complete.
  Tests now patch and assert the completed-workspace `ComposeManager` callback
  instead of reverting the implementation to direct monitor subprocess teardown.
- Keep assertions that successful completion requests compose teardown,
  including the exception-swallowing path: Complete.
  The affected integration tests assert recorded GC-backed teardown calls, and
  the exception test raises from the callback after recording the call.
- Keep the merge coordinator assertion that merge recheck and merge happen under
  the coordinator while completed-workspace compose teardown happens after the
  coordinator is released: Complete.
  The unit test records coordinator activity at teardown time and asserts it is
  inactive.
- Avoid real Docker in these unit/integration tests: Complete.
  The affected tests patch completed-workspace `ComposeManager` and no longer
  rely on a local Docker daemon or a queued direct `docker compose down`.
- Run focused pytest commands for the reported failures after edits: Complete.
- Record focused validation evidence and note broad validation ownership:
  Complete.

## Evidence

Files changed:

- `tests/shared/monitor_runner.py`
- `tests/unit/runtime/_monitor_runner_fixtures.py`
- `tests/unit/runtime/test_merge_coordinator_runner.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
- `plans/CI_PR396_MONITOR_COMPLETION_TEARDOWN_PLAN.md`
- `plans/CI_PR396_MONITOR_COMPLETION_TEARDOWN_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_happy_merge_tears_down_compose tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_short_circuit_completed_tears_down_compose tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_merge_blocked_notify_human_tears_down_after_external_merge tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_plain_notify_human_tears_down_after_external_merge tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_teardown_raised_exception_swallowed -q`
  passed: 5 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_coordinator_runner.py::TestMergeCoordinatorRunner::test_auto_merge_wraps_final_recheck_and_merge_in_coordinator -q`
  passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack -q`
  passed: 7 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q`
  passed: 13 passed.
- `uv run --python 3.12 --extra dev ruff check tests/shared/monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py tests/unit/runtime/test_merge_coordinator_runner.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
  passed.

Full AWF/GitHub-owned validation, full coverage gates, whole-repository pytest,
and broad frontend/build validation were not run in the agent phase per the AWF
workspace contract.

## Remaining Gaps

None.
