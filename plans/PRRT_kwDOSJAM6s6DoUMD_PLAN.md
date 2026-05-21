# PRRT_kwDOSJAM6s6DoUMD Plan

## Problem Statement

The PR review reports that preserved active recovery silently gives up on clean
committed work when no executor is configured. In that path
`_recover_preserved_active_execution` returns `False` without writing a salvage
event, allowing the stale-active cleanup path to fail the workspace and discard
committed work.

## Scope

Change only the preserved active recovery path for committed work with an absent
executor, plus focused unit coverage and this review-thread plan/validation
record.

## Requirements Checklist

- Add or update a regression test proving committed preserved work with no
  executor writes `workspace.active_execution_salvage_blocked`.
- Preserve existing policy that no validation request event or validate
  operation is created when no executor exists.
- Ensure recovery returns `True` for this blocked state so stale-active cleanup
  remains gated until an executor is available.
- Keep the stale-failure behavior for already-requested validation salvage with
  no executor unchanged.
- Run the narrow affected test selection and lint for changed Python files.
- Commit the resulting scoped changes locally.

## Implementation Steps

1. Update the existing no-executor committed-work regression to expect a blocked
   salvage event instead of a silent `False` recovery.
2. Confirm that test fails against the current implementation.
3. In the committed classification branch, record salvage blocked with the
   committed classification and return `True` when `_executor` is absent.
4. Re-run the focused tests and ruff.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "committed_work_without_executor_writes_blocked_salvage or validation_salvage_without_executor"`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`

Pass criteria: the targeted no-executor recovery tests pass and ruff reports no
issues for touched Python files.
