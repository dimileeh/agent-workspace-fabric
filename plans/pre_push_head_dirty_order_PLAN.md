# Pre-Push Head Dirty Order Plan

## Problem Statement

PR monitor pre-push validation currently captures local `HEAD` before checking whether the validation worktree is already dirty. If `HEAD` capture fails on a dirty worktree, the monitor reports a pre-push infrastructure failure instead of the existing validation worktree pre-existing dirty reason.

## Scope

- Keep the change limited to PR monitor pre-push validation behavior.
- Preserve existing dirty-worktree details when `HEAD` capture succeeds.
- Do not run broad AWF/GitHub-owned validation; use focused unit tests only.

## Requirements

- Add a regression test for a dirty pre-push validation worktree where `HEAD` capture fails.
- Ensure dirty worktree detection runs before any failure from `HEAD` capture can mask it.
- Preserve `PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED` when the worktree is clean but `HEAD` capture fails.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Add a focused unit regression in `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.
2. Run only the new test and confirm it fails before the code change.
3. Reorder `_run_pre_push_validation` so cleanliness is checked before `HEAD` failure handling, while still trying to include `HEAD` in dirty failure details when available.
4. Run the new regression and nearby targeted pre-push validation tests.
5. Record validation evidence in `plans/pre_push_head_dirty_order_VALIDATION.md`.
