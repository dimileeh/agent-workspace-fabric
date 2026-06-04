# CI PR396 Monitor Completion Teardown Plan

## Problem Statement And Scope

PR #396 CI fails in the Python full-coverage job because completed PR-monitor
tests still assert the pre-GC cleanup path: a direct `docker compose down` call
through the monitor's fake command runner. The current implementation completes
workspaces through filesystem GC with a completed-workspace compose teardown
callback, so the tests hit real Docker when they do not patch that callback and
the merge-coordinator test no longer observes a fake-runner compose command.

Scope is limited to repairing the stale test contracts for the reported PR
monitor completion and merge-coordinator failures. Do not change workflow or
quality-gate configuration, and do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- Reproduce the reported focused failures before edits.
- Keep completed-workspace cleanup routed through filesystem GC and the
  completed-workspace compose teardown callback.
- Keep assertions that successful completion requests compose teardown, including
  the exception-swallowing path.
- Keep the merge coordinator assertion that merge recheck and merge happen under
  the coordinator while completed-workspace compose teardown happens after the
  coordinator is released.
- Avoid real Docker in these unit/integration tests.
- Run focused pytest commands for the reported failures after edits.
- Record focused validation evidence and note that broad AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Add a shared PR-monitor test helper that patches the completed-workspace
   `ComposeManager` and records `teardown_project` calls.
2. Reuse that helper from existing unit fixtures instead of keeping the helper
   unit-only.
3. Update the five reported integration tests to assert recorded GC-backed
   compose teardown calls instead of fake-runner `docker compose down` calls.
4. Update the reported merge-coordinator test to assert the recorded compose
   teardown happens while the coordinator is inactive.
5. Keep edits scoped to test support, reported tests, and this plan/validation
   documentation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_happy_merge_tears_down_compose tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_short_circuit_completed_tears_down_compose tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_merge_blocked_notify_human_tears_down_after_external_merge tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_plain_notify_human_tears_down_after_external_merge tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestCompleteWorkspaceTearsDownComposeStack::test_teardown_raised_exception_swallowed -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_coordinator_runner.py::TestMergeCoordinatorRunner::test_auto_merge_wraps_final_recheck_and_merge_in_coordinator -q`
  passes.
