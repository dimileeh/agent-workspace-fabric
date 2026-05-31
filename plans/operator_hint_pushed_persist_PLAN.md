# Operator Hint Pushed Persist Plan

## Problem Statement And Scope

The PR monitor clears a pending operator remonitor hint in memory after a
successful repair push, but the pushed path does not persist that cleared state
before returning to the outer loop. A runner crash in that window can reload the
same hint as pending and rerun the repair.

Scope is limited to the operator-hint branch in the PR monitor runner and a
focused regression test.

## Requirements Checklist

- Persist processed operator-hint state immediately when an operator-hint repair
  successfully pushes commits.
- Preserve the existing terminal/no-op persistence behavior.
- Add a regression test that fails without the pushed-path persistence.
- Do not run broad AWF/GitHub validation; use targeted tests only.

## Implementation Steps

1. Add a focused unit test for the successful pushed operator-hint path.
2. Confirm the new test fails on the current implementation.
3. Update the monitor loop to persist successful pushed operator-hint state
   before recording the succeeded operation and returning.
4. Re-run the focused regression test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k pushed_processed_status_is_persisted_before_return`
  - Passes after implementation.
  - Fails before implementation because the pending hint remains persisted.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
