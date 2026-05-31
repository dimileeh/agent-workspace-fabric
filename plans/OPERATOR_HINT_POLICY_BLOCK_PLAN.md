# Operator Hint Policy Block Plan

## Problem Statement And Scope

An operator remonitor hint can remain in `pending` status when the repair agent
is blocked by monitor policy. The monitor loop then dispatches the same
`AddressOperatorHint` action again instead of falling through to human
notification. The change is scoped to operator-hint repair handling and its
focused regression coverage.

## Requirements Checklist

- Add a regression test that reproduces a policy-blocked operator-hint repair.
- Transition the pending operator hint to a terminal human-facing status when
  `_MonitorPolicyBlockedError` is raised during the operator hint cycle.
- Preserve the policy block reason in the hint status reason and returned
  result stderr.
- Avoid changing unrelated review-thread, push, or protected-scope behavior.
- Run only targeted checks for the changed behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   that monkeypatches the CLI verdict call to raise `_MonitorPolicyBlockedError`.
2. Confirm the new test fails with the existing implementation because the hint
   remains `pending`.
3. Update `src/awf/runtime/pr_monitor_runner/operator_hints.py` to mark the
   operator hint as `needs_human` with a policy-block reason before returning.
4. Re-run the targeted regression test.
5. Create `plans/OPERATOR_HINT_POLICY_BLOCK_VALIDATION.md` with requirement
   status and command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k policy_block`
  - Passes after implementation.
  - Fails before implementation with an assertion that the pending hint was not
    transitioned.

Full AWF/GitHub validation is intentionally not run in this agent phase.
