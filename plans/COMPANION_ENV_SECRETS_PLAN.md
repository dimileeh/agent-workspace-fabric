# Companion Environment Secrets Plan

## Summary

Fix issue #290 by keeping companion `environment` literal-only while adding an
AWF-owned `environment_secrets` field that lets callers pass host-managed env
secrets to cross-repo companion services without raw values in dispatch
payloads.

## Implementation

- Add `environment_secrets` to companion create schemas as a mapping from target
  env var name to an env-backed secret reference.
- Support only `provider: env` and `kind: env` in this slice.
- Reject invalid target/source env names and target overlap with literal
  `environment`.
- Persist only secret references in `task_policy`.
- Resolve companion env secrets during stack launch from the worker environment.
- Render AWF-generated Compose placeholders like `${ANTHROPIC_API_KEY}` into
  companion service env.
- Fail required missing source env with `COMPANION_ENV_SECRET_SOURCE_MISSING`
  and non-secret diagnostics.
- Omit optional missing source env and record metadata.
- Keep the current interpolation rejection for user-authored literal
  `environment`, `command`, `healthcheck_cmd`, and paths.
- Document literal env versus secret env references and regenerate OpenAPI.

## Validation

- Targeted schema tests for acceptance/rejection and interpolation behavior.
- Targeted companion materialization and stack launcher tests for required and
  optional env secret resolution.
- Targeted compose rendering test for DinD companion placeholder behavior.
- Run focused pytest, ruff/mypy on touched Python files, and OpenAPI drift
  check.
