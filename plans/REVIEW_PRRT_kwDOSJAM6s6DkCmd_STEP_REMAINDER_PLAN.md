# Review PRRT_kwDOSJAM6s6DkCmd Step Remainder Plan

## Problem Statement and Scope

An unresolved review thread reports that workflow step display fields are compared twice:
`_step_identity` uses `id` and `name` to match existing workflow steps, but
`_step_remainder` still includes those keys when checking for disallowed
structural changes. This can flag a pure display-name update on a stable step
`id` as "workflow step changed outside allowed fields."

Scope is limited to the protected quality-gate workflow step comparison and its
unit coverage.

## Requirements Checklist

- Add a regression test proving a workflow step with the same `id` can update
  only its display `name` without producing a protected quality-gate violation.
- Keep blocking behavior for real structural step changes such as `env`
  additions intact.
- Update `_step_remainder` so identity/display keys do not cause remainder
  mismatches after step matching.
- Run the narrow unit test proving the regression and nearby structural-change
  coverage.

## Implementation Steps

1. Add the failing workflow-step rename regression test.
2. Confirm the new test fails against the current implementation.
3. Ignore `id` and `name` inside `_step_remainder`.
4. Run focused tests for the new regression and existing workflow shape-change
   coverage.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "same_id or existing_workflow_job_and_step_shape_changes"`

Pass criteria: the focused tests pass, including the new rename regression and
the existing `env` structural-change blocking case.
