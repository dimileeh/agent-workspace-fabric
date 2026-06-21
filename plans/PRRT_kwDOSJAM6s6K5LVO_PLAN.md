# PRRT_kwDOSJAM6s6K5LVO Plan

## Problem Statement and Scope

The sync-base conflict path currently handles provider agent failures before
running the post-agent dirty-worktree commit sink. If provider recovery raises a
retry, fallback, or auth exception, the sink never runs its post-agent mirror
hooks-path repair and missing-HEAD recovery guards. Scope is limited to the
ordering in `src/awf/runtime/pr_monitor_runner/remote_ops.py` and a focused
regression test.

## Requirements Checklist

- Verify the review claim against current code.
- Add a focused regression test that fails when provider recovery runs before
  `_commit_dirty_worktree` in the sync-base conflict path.
- Change the sync-base conflict path so `_commit_dirty_worktree` runs before
  provider retry/fallback/auth handling can short-circuit.
- Preserve provider recovery propagation after the post-agent guard runs.
- Run only targeted validation for the changed behavior; broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Inspect the line-targeted code and existing sync-base tests.
2. Add a regression test in the existing sync-base operation-start-head test
   module using local fakes.
3. Move `_handle_provider_agent_run_error()` after the commit sink in the
   sync-base conflict path.
4. Run the new targeted test, then the focused sync-base test module if needed.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
- Pass criteria: the new regression and existing focused sync-base tests pass.
