# PRRT_kwDOSJAM6s6DbbGX Informational Job Permissions Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DbbGX` reports that added informational workflow
jobs are allowed when they omit `permissions`, but rejected when they explicitly
deny all token permissions with `permissions: {}` or request only read-only
`contents: read`. This inverts least-privilege handling for informational jobs.

Scope is limited to the protected workflow classifier and focused unit coverage.

## Requirements Checklist

- Allow added informational jobs with no job-level permissions block.
- Allow added informational jobs with explicit empty or read-only allowed
  permission scopes.
- Preserve allowance for comment-oriented informational jobs that request
  `issues` or `pull-requests` write permission.
- Continue blocking broad or risky job permissions such as `contents: write`,
  `write-all`, invalid levels, and unknown scopes.
- Run focused quality-gate tests and lint for touched files.

## Implementation Steps

1. Add regression coverage showing `permissions: {}` and `contents: read` are
   allowed for added informational jobs.
2. Confirm the new regression fails before the implementation change.
3. Update `_informational_job_permissions_are_safe` so accepted explicit
   permission mappings do not require a comment write scope.
4. Keep existing validation for allowed scopes and levels.
5. Run targeted tests, lint, and record validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_restricted_permissions_is_allowed -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_privileged_fields_is_blocked tests/unit/control/test_quality_gates.py::test_private_workflow_shape_helpers_cover_empty_and_invalid_edges -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
