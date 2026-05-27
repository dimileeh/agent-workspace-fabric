# PRRT_kwDOSJAM6s6FLMwh Companion Git Branch Name Plan

## Problem Statement And Scope

The review thread reports that companion names accepted by the public request
schema can still be invalid when interpolated into the deterministic companion
branch name `awf/<workspace>/companion/<name>`. Names such as `foo.lock`,
`foo..bar`, and `.` currently pass `ServiceName` validation and fail later when
provisioning calls `git worktree add -b`.

Scope is limited to rejecting Git-invalid companion names at request
validation and adding focused regression coverage.

## Requirements Checklist

- Reject companion request names that cannot be used as the final Git branch
  path component.
- Preserve existing valid service names such as `backend`.
- Keep the fix in the companion request validation path so provisioning is not
  reached for invalid names.
- Add a regression test for the reported invalid names.
- Run focused schema tests only; full AWF/GitHub validation is managed after
  agent completion.

## Implementation Steps

1. Add a focused failing schema regression for `foo.lock`, `foo..bar`, and `.`.
2. Update companion name validation to reject names that violate the Git branch
   component constraints reachable under the existing `ServiceName` pattern.
3. Re-run the focused regression test and the nearby schema edge test file.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_git_invalid_names -q`
  - Before implementation: fails because invalid names are accepted.
  - After implementation: passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Pass criteria: focused schema edge tests pass.
