# PRRT_kwDOSJAM6s6KxJtv Plan

## Problem Statement and Scope

The review thread reports that `_run_sync_base` captures `operation_start_head`
but does not pass it to `_validated_git_push_result`. This can make pre-push
validation recover from the current post-repair HEAD instead of the known-good
operation start. Scope is limited to verifying and fixing that call path.

## Requirements Checklist

- Verify the reported `_run_sync_base` call site against current code.
- Thread the captured `operation_start_head` into `_validated_git_push_result`.
- Add or update a focused regression test proving the value is passed.
- Run targeted validation only; full AWF/GitHub validation remains managed by
  AWF after agent completion.
- Commit the scoped fix locally.

## Implementation Steps

1. Inspect `src/awf/runtime/pr_monitor_runner/remote_ops.py` around the reported
   lines and existing focused tests for `_run_sync_base`.
2. Add a focused `_run_sync_base` test to assert the push validator receives the
   captured operation-start head.
3. Add `operation_start_head=operation_start_head` to the final validated push
   call in `_run_sync_base`.
4. Run the targeted unit test for the changed behavior.
5. Record validation evidence in `plans/PRRT_KWDOSJAM6S6KXJTV_VALIDATION.md`.

## Assumptions/Changes

- The existing `_run_sync_base` task-tag test file is owned by `root:root` in
  this workspace and cannot be edited by the agent user. Add the regression in a
  new focused test file instead.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  passes.
- No broad AWF/GitHub-owned validation is run inside this agent phase.
