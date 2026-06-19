# Pre-Push Recovered HEAD Protected Scope Plan

## Problem Statement and Scope

An unresolved PR review thread reports that `_run_pre_push_validation` recovers a missing
HEAD object and then only verifies that a recovered diff is computable. If the recovered
delta touches protected files, the clean pre-push validation path can continue without the
ownership and protected-scope repair gates used by `_commit_dirty_worktree` and the fix-pass
path.

Scope is limited to `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and focused
unit coverage for this recovered-HEAD path.

## Requirements Checklist

- When a missing pre-push HEAD is recovered to a different commit, compute the recovered
  delta before validation.
- If recovered changed paths exist, run agent runtime ownership repair before invoking the
  protected-scope repair gate.
- Invoke the existing protected-scope repair hook for the recovered committed paths before
  validation starts.
- If the recovered diff cannot be computed, preserve the existing fail-closed behavior.
- If protected-scope repair blocks, do not run validation and return a structured failure.
- Keep the change minimal and avoid broad validation; AWF/GitHub own broad gates after this
  agent phase.

## Implementation Steps

1. Add a focused regression test for a recovered missing HEAD with changed paths.
2. Confirm the test fails before implementation when practical.
3. Add the recovered-delta ownership and protected-scope repair gate to the clean pre-push
   recovery branch.
4. Run the focused unit test(s) for the changed behavior.
5. Write validation results to `plans/PRE_PUSH_RECOVERED_HEAD_PROTECTED_SCOPE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  should pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase per the workspace
  contract.
