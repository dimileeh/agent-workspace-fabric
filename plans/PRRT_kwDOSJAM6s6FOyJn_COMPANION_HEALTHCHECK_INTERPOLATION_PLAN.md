# PRRT_kwDOSJAM6s6FOyJn Companion Healthcheck Interpolation Plan

## Problem Statement And Scope

Public companion requests currently reject Docker Compose interpolation syntax in companion environment values, but the free-form `healthcheck_cmd` field is still accepted and later rendered into Compose. A request such as `curl -H "Authorization: ${GITHUB_TOKEN}" ...` can therefore expose compose-process environment secrets through Compose interpolation.

Scope is limited to public companion request validation for `healthcheck_cmd` and focused regression coverage.

## Requirements Checklist

- Reject Docker Compose interpolation syntax in public companion `healthcheck_cmd` values.
- Preserve existing accepted companion healthcheck commands that do not contain interpolation syntax.
- Add a regression test for the review-reported secret interpolation shape.
- Keep the change scoped to companion API schema validation and its focused tests.

## Implementation Steps

1. Add a failing test in the API schema edge suite for a companion healthcheck command containing `${GITHUB_TOKEN}`.
2. Update `WorkspaceCompanionRequest` validation to reuse the existing Compose interpolation detector for `healthcheck_cmd`.
3. Run the focused regression test and a narrow companion schema test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Passes after the validator rejects the review-reported healthcheck command.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Passes without regressions in related schema edge validation.

Full AWF/GitHub validation remains managed by AWF after agent completion per the workspace contract.
