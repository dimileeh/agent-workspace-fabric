# PRRT_kwDOSJAM6s6LqOp2 Plan

## Problem Statement and Scope

The review thread reports duplicated atomic clear logic in
`src/awf/runtime/pr_monitor_runner/merge_attention.py`: the stale merge-attention
branch and the queue-wait clear helper both perform the same locked workspace row
transaction to remove the merge-block marker and clear workspace attention.

Scope is limited to extracting the shared database transaction into a private
coroutine and calling it from the existing two clear paths. No behavior change,
branch management, push, or broad validation is in scope.

## Requirements Checklist

- Preserve the existing in-memory marker clear behavior in both callers.
- Preserve the existing single `get_for_update` transaction that removes the
  persisted merge-block marker and clears workspace attention.
- Keep the missing-workspace no-op behavior.
- Avoid unrelated refactors or test rewrites.
- Run focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Replace the stale branch's inline database clear body with a call to a shared
   private coroutine.
2. Update `_clear_merge_block_attention_and_workspace_attention_durably` to clear
   in-memory state and delegate the row transaction to the shared coroutine.
3. Add the private coroutine containing the common locked row transaction.
4. Run focused tests covering merge attention atomic-clear behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Passes, confirming the existing atomic-clear, rollback, missing-workspace,
    and helper behavior is preserved.
