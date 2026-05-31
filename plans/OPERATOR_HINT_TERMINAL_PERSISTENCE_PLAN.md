# Operator Hint Terminal Persistence Plan

## Problem Statement And Scope

An inline review thread reports that operator remonitor hints marked
`needs_human` during terminal protected-scope failures are not persisted before
the monitor transitions the workspace to failed. The scope is limited to the
`AddressOperatorHint` execution path in the PR monitor runner and focused unit
coverage for durable operator-hint state.

## Requirements Checklist

- Add a regression test proving terminal operator-hint protected-scope failures
  persist the hint status as `needs_human`.
- Cover both terminal protected-scope reason codes:
  `PROTECTED_SCOPE_PUSH_BLOCKED` and `PROTECTED_SCOPE_DIFF_UNAVAILABLE`.
- Persist monitor state before terminal failure handling in the
  `AddressOperatorHint` branch.
- Keep changes scoped to PR monitor runtime behavior, its tests, and plan
  documentation.
- Use focused validation only; AWF/GitHub own broad validation after this agent
  phase.

## Implementation Steps

1. Add a parametrized unit regression in
   `tests/unit/runtime/test_pr_monitor_operator_hints.py` that seeds a pending
   operator hint, simulates `_run_operator_hint_cycle` marking it
   `needs_human`, returns a terminal protected-scope push failure, and verifies
   the persisted hint status after `_execute`.
2. Run the focused test and confirm it fails before the production change.
3. Update `src/awf/runtime/pr_monitor_runner/loop.py` so the
   `AddressOperatorHint` terminal push-failure branch persists state before
   calling `_terminate_failed`.
4. Re-run the focused regression.
5. Record validation evidence in
   `plans/OPERATOR_HINT_TERMINAL_PERSISTENCE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passes with the new regression included.
- Full AWF/GitHub validation is intentionally not run locally; AWF owns broad
  post-agent validation and merge gating.
