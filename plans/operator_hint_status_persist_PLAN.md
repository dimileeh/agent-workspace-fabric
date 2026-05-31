# Operator Hint Status Persist Plan

## Problem Statement And Scope

An operator hint repair can conclude without a pushed commit and mark the pending
operator hint as `needs_human` or `agent_failed`. The direct
`AddressOperatorHint` execution path currently returns before persisting that
terminal hint status, leaving a restart window where the database still records
the hint as `pending`.

Scope is limited to the PR monitor operator-hint execution path and focused unit
coverage for that persistence edge. No protected workflow, quality-gate, or
repository-wide validation configuration files are in scope.

## Requirements Checklist

- Add a regression test proving non-pushed operator hint results persist
  terminal hint status during `_execute`.
- Persist terminal operator hint status before returning from the non-pushed,
  non-failed `AddressOperatorHint` path.
- Preserve existing terminal-failure behavior and pushed-fix behavior.
- Run only focused local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   that seeds a pending operator hint, simulates a non-pushed result that marks
   the hint `needs_human` or `agent_failed`, and asserts the database stores the
   terminal status after `_execute`.
2. Run the new test to confirm it fails before the code change.
3. Update `src/awf/runtime/pr_monitor_runner/loop.py` to persist terminal
   operator hint status for non-pushed, non-failed results before the branch
   returns.
4. Re-run the focused operator-hint tests touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`

Pass criteria: the focused operator hint unit tests pass. Full AWF/GitHub
validation is intentionally not run in the agent phase per the workspace
contract.
