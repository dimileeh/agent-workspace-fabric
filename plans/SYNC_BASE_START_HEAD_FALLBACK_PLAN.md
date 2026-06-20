# Sync-Base Start Head Fallback Plan

## Problem Statement

The sync-base repair path currently trusts `pr_head_sha` directly when it is
available. That bypasses the shared start-head capture helper that verifies the
local worktree HEAD when possible.

## Scope

- Update only the sync-base start-head capture behavior.
- Add a focused regression test for the `pr_head_sha` path.
- Do not run broad AWF/GitHub-owned validation.

## Requirements

- `sync_base` always calls `_repair_operation_start_head_result`.
- `pr_head_sha` is passed only as `fallback_head_sha`.
- A helper failure result still short-circuits the sync-base operation.
- The verified helper head is threaded to the eventual validated push.

## Implementation Steps

1. Add a failing unit test that calls `_run_sync_base` with `pr_head_sha` and
   asserts the helper receives it as fallback.
2. Change `_run_sync_base` to use the helper unconditionally.
3. Run the focused unit test file.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion.
