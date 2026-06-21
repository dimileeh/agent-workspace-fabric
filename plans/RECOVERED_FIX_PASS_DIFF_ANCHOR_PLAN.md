# Recovered Fix-Pass Diff Anchor Plan

## Problem Statement and Scope

An unresolved PR review thread reports that pre-push validation fix-pass recovery
can fall back from a missing `fix_start_head` object to the open merge-candidate
SHA, but the recovered-delta protected-scope diff still compares against the
original missing `fix_start_head`. That makes successful fallback recovery fail
before protected-scope validation can run.

Scope is limited to the fix-pass missing-HEAD recovery anchor logic and a focused
regression test.

## Requirements Checklist

- Use the actual recovery anchor for recovered-delta comparison.
- Only run recovered-delta validation when `recovered != recovery_head`.
- Preserve the existing behavior for the non-fallback path.
- Add focused regression coverage for fallback recovery using the merge-candidate
  SHA as the diff base.
- Do not run broad AWF/GitHub-owned validation; record focused checks only.

## Implementation Steps

1. Read the recovery logic and existing tests for recovered-delta behavior.
2. Add a failing regression test for a missing `fix_start_head` where recovery
   falls back to the open merge-candidate SHA.
3. Update the fix-pass recovery logic to store the recovery anchor and use it as
   the recovered-delta diff/protected-scope base.
4. Run the focused test file or targeted test case.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_005.py -q`
- Pass criteria: focused tests pass, and the new regression observes the diff
  range anchored at the fallback merge-candidate SHA.
