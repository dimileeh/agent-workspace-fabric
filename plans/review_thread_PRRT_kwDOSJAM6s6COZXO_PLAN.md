# Review Thread PRRT_kwDOSJAM6s6COZXO Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/control/executor.py`.
The review identifies two executor issues:

- `_validation_run_command_records` redundantly checks the coverage command
  after `_should_run_local_coverage(profile)` has already proven it exists.
- `_validation_tier_for_workspace` should account for successful validate
  operations that requested a higher validation tier than the resolved profile,
  preventing monitor recovery from repeatedly dispatching validation for a tier
  that has already succeeded.

Scope is limited to executor helper behavior and focused unit coverage.

## Requirements Checklist

- Add a regression test showing successful validate operations raise the
  effective validation tier.
- Preserve the existing task-class tier floor behavior.
- Ignore unsuccessful, malformed, and unrelated operation records when deriving
  the tier.
- Remove the redundant coverage command guard without changing coverage command
  record behavior.
- Run the narrow focused test file that covers these helpers.

## Implementation Steps

1. Update `tests/unit/control/test_executor_coverage_edges.py` with a focused
   `_validation_tier_for_workspace` regression.
2. Update `src/awf/control/executor.py` so `_validation_tier_for_workspace`
   derives the maximum tier from the profile, task class, and successful
   validate operations.
3. Simplify `_validation_run_command_records` by relying on
   `_should_run_local_coverage(profile)` before dereferencing the command.
4. Run the targeted unit tests and fix any failures.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q
```

Pass criteria: the focused test file passes, including the new regression.
