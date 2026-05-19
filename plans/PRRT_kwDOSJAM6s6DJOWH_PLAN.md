# PRRT_kwDOSJAM6s6DJOWH Plan

## Problem Statement And Scope

An unresolved review thread reports that `collect_core_readiness_report()` ignores
`compose_env_file` for provider readiness when direct callers omit
`provider_environ`. The fix is scoped to readiness provider-environment
resolution and its regression coverage.

## Requirements Checklist

- Reproduce the reviewer issue with a unit test.
- Preserve explicit `provider_environ` precedence.
- When `provider_environ` is omitted, load provider credentials from the supplied
  Compose env file and merge them with the caller environment.
- Continue passing the resolved environment to both status and doctor
  collection.
- Run the narrow unit test proving the regression.

## Implementation Steps

1. Add a failing regression test in `tests/unit/service/test_readiness.py`.
2. Add a small readiness helper that resolves the provider environment from
   `provider_environ`, `environ`, `compose_file`, and `compose_env_file`.
3. Use the helper inside `collect_core_readiness_report()`.
4. Run the targeted test and update validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py -q`
  must pass.
