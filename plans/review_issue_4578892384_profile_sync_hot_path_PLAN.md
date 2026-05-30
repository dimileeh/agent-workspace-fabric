# Review issue 4578892384 profile sync hot path plan

## Problem statement and scope

The executor resolves a workspace profile, then always calls `_sync_resolved_profile`.
When the claimed workspace already carries a frozen `resolved_profile`, `_profile_for_workspace`
has already loaded that snapshot and the sync call performs redundant database reads/writes.

Scope is limited to the main execution flow hot path called out by the review comment.

## Requirements checklist

- Add a regression test showing `execution_flow.execute` does not call `_sync_resolved_profile`
  when the claimed workspace already has `resolved_profile`.
- Keep first-write-wins persistence for workspaces without `resolved_profile`.
- Avoid broad validation; run only targeted tests for the touched behavior.
- Do not switch branches or push.

## Implementation steps

1. Add a focused unit test in the executor runtime profile snapshot test module.
2. Confirm the new test fails against the current unconditional sync behavior.
3. Guard the `_sync_resolved_profile` call in `execution_flow.execute` behind a missing-snapshot check.
4. Run the targeted unit test file or specific tests covering the new regression and existing missing-snapshot sync behavior.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`

Pass criteria: targeted tests pass, and full AWF/GitHub validation remains delegated to AWF after agent completion.
