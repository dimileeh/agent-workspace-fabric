# PRRT_kwDOSJAM6s6DjxpL Plan

## Problem Statement and Scope

The worker-restart replacement salvage path cancels a preserved active workspace
and creates a retry operation when no usable work can be recovered. For
`validating` and `pushing` source workspaces, an existing active validate or push
operation can remain pending/running after the source workspace is cancelled.

Scope is limited to replacement salvage for preserved active executions in
`src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test proving replacement salvage for non-running
  `validating`/`pushing` workspaces cancels the superseded active validate/push
  operation.
- Reuse the existing `_cancel_superseded_active_execution_operations` helper so
  cancellation result metadata remains consistent with validation salvage.
- Preserve the replacement retry operation and replacement workspace behavior.
- Keep the fix scoped and avoid changing unrelated recovery paths.

## Implementation Steps

1. Add a failing unit test near the preserved active no-work replacement tests.
2. Run the narrow test to confirm the current bug when practical.
3. Call `_cancel_superseded_active_execution_operations` after the replacement
   retry operation is created, passing the retry operation as the replacement
   operation and the preservation event as the cycle marker.
4. Add cancellation metadata to the replacement salvage payload/event.
5. Re-run the focused test and a nearby preserved-active worker test slice.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_without_usable_work'`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_without_usable_work'`
  passes.
