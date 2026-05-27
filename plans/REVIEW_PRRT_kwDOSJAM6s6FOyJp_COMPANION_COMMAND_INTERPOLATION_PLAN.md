# Companion Command Interpolation Review Fix Plan

## Problem statement and scope

Review thread `PRRT_kwDOSJAM6s6FOyJp` reports that public companion requests reject Docker Compose interpolation syntax in environment values and healthcheck commands, but still accept the `command` field unchanged. Because companion commands are rendered into the Compose service definition, `${NAME}` and `$NAME` syntax in that field can be interpolated from the AWF process environment.

Scope is limited to the public companion request schema and its focused regression coverage.

## Requirements checklist

- Add a regression test proving companion `command` rejects Docker Compose interpolation syntax.
- Preserve existing accepted companion command behavior except for interpolation-bearing values.
- Reuse the existing interpolation detector so escaped `$$` behavior remains consistent with environment and healthcheck validation.
- Do not run broad AWF/GitHub-owned validation; use focused local checks only.

## Implementation steps

1. Add a failing case to the existing invalid companion public-contract tests for `command: "sh -c 'echo ${GITHUB_TOKEN}'"`.
2. Run the focused test module or specific test to confirm the new case fails before implementation.
3. Add a `command` field validator in `WorkspaceCompanionRequest` using `_value_has_compose_interpolation`.
4. Re-run the focused test to confirm the regression is fixed.
5. Record validation evidence in the matching validation document.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Pass criteria after implementation: all parameter cases in the focused test pass.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
