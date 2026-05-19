# PRRT_kwDOSJAM6s6DUTJI Privileged Job Fields Plan

## Problem Statement And Scope

An unresolved review thread reports that protected workflow classification allows
newly added informational-looking jobs with job-level fields such as
`permissions`, `needs`, `if`, or `environment`. Those fields can change
workflow execution or security posture, so unowned protected workflow changes
should reject them even when the job label and steps look informational.

Scope is limited to the protected workflow classifier and its unit tests.

## Requirements Checklist

- Add a regression test proving an added informational job with privileged
  job-level fields is blocked.
- Preserve the existing allowance for minimal step-based informational jobs.
- Preserve existing blocks for reusable workflow jobs and step-level `uses`.
- Make a narrow fail-closed classifier change for newly added informational jobs.
- Run targeted tests covering the changed quality-gate behavior.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_quality_gates.py` for an
   added notify/comment job that includes privileged job-level keys.
2. Run the new test and confirm it fails before implementation.
3. Restrict added informational jobs in
   `src/awf/control/quality_gates.py` to a minimal allowlist of job-level keys.
4. Run the new regression and the quality-gate unit test file.
5. Record requirement-by-requirement validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_privileged_fields_is_blocked -q`
  fails before the implementation change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
