# Failure Causality Review 4445667428 Plan

## Problem Statement And Scope

Address PR review comment `issue:4445667428`, limited to the two reported
failure-causality concerns:

- Make cleanup failure secondary-payload preservation variables explicitly
  initialized in `src/awf/service/controls.py`.
- Prevent `load_failure_causality_snapshot` from querying historical failed
  validation runs when no failed workspace event belongs to the current failure
  epoch.

No branch changes, pushes, or GitHub comments are in scope.

## Requirements Checklist

- Preserve existing primary/secondary failure-causality behavior.
- Add or update a regression test for the stale validation-run edge case.
- Keep the controls cleanup path behavior unchanged while making the invariant
  self-evident to static analysis and future edits.
- Run the narrow relevant tests and static checks practical for this change.
- Commit the local fix with a conventional commit message referencing the
  review comment id.

## Implementation Steps

1. Add explicit default values for `preserved_secondary_failure` and
   `preserved_secondary_failures` before the guarded cleanup preservation block.
2. Gate the failed validation-run lookup in `load_failure_causality_snapshot`
   so it only runs when there is a current-epoch failed workspace event.
3. Add a regression test proving a same-timestamp epoch reset does not attach a
   stale validation run when live workspace fields still report validation
   failure.
4. Run focused unit tests for failure causality and controls, then run targeted
   lint/type checks if practical.
5. Write `plans/FAILURE_CAUSALITY_REVIEW_4445667428_VALIDATION.md` with
   requirement status and evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py -q`
  should pass or any unrelated environmental failure must be documented.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py src/awf/service/controls.py`
  should pass or any existing scope/config limitation must be documented.
