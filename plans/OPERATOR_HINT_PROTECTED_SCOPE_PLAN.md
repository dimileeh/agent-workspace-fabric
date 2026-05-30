# Operator Hint Protected-Scope Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F5w90` reports that operator-hint repair does
not catch `ProtectedScopeDiffError` raised while committing dirty agent changes.
The comment and CI repair paths already convert this failure into the protected
scope diff-unavailable push result. Operator-hint repair should do the same so
the monitor can finish the repair operation and surface the protected-file
approval blocker.

Scope is limited to the operator-hint repair path and a focused regression test.

## Requirements Checklist

- Catch `ProtectedScopeDiffError` from the operator-hint CLI/dirty-commit path.
- Return the existing protected-scope diff-unavailable push result for the
  workspace and remote branch.
- Preserve existing handling for policy blocks, ownership repair failures,
  verdicts, and successful pushes.
- Add a regression test for operator-hint repair receiving
  `ProtectedScopeDiffError`.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add a failing regression test that invokes `_run_operator_hint_cycle` with
   `_invoke_cli_for_verdict_result` raising `ProtectedScopeDiffError`.
2. Update `src/awf/runtime/pr_monitor_runner/operator_hints.py` to catch the
   error and delegate to `_protected_scope_diff_unavailable_push_result`.
3. Run the focused test file or focused test case that covers the change.
4. Record validation evidence in
   `plans/OPERATOR_HINT_PROTECTED_SCOPE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`

Pass criteria: the operator-hint regression passes, and no broader validation is
run inside this AWF agent phase.
