# Review 4620252998 Empty-Plan Compose Teardown Plan

## Problem statement and scope

The review reports that completed-workspace filesystem GC can return an empty
candidate plan before invoking the compose teardown callback. In the PR monitor
completion path, that means the Docker compose stack is not torn down and no
`monitor.compose_teardown_ok` or `monitor.compose_teardown_failed` event is
emitted.

Scope is limited to preserving compose teardown observability for the
completed-workspace empty-plan path. Filesystem path deletion, lease revocation,
reservation release, and protected/preserved workspace behavior should not be
widened.

## Requirements checklist

- Reproduce the empty-plan completion path with a focused regression test.
- Ensure a compose teardown callback is invoked when the targeted workspace GC
  execution has no candidate row to iterate.
- Preserve existing teardown failure gating for normal candidate-backed GC.
- Do not run broad AWF/GitHub-owned validation in the agent phase.
- Keep the change scoped to GC/lifecycle behavior and focused tests.

## Implementation steps

1. Add a focused regression test for completed-workspace GC with no matching
   workspace row, asserting compose teardown and monitor logging still occur.
2. Confirm the regression test fails before implementation when practical.
3. Add a narrow empty-plan compose teardown fallback that does not add path,
   lease, or reservation side effects.
4. Run the focused regression test and a nearby completion-GC test.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tears_down_compose_when_plan_is_empty -q`
  - Fails before implementation because no compose teardown call or monitor
    event is emitted.
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tears_down_compose_when_plan_is_empty tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails -q`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion
per the workspace contract.
