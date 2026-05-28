# PRRT_kwDOSJAM6s6FUXWQ Plan

## Problem Statement and Scope

The `environment_secrets` OpenAPI schema documents the allowed key shape through
`patternProperties`, but its `propertyNames` schema only has length bounds. This
lets OpenAPI consumers miss the same environment-variable-name rule that runtime
validation already enforces.

Scope is limited to the companion request schema, generated OpenAPI artifact, and
focused regression tests for this review thread.

## Requirements Checklist

- Add the environment-variable key regex to `environment_secrets.propertyNames`.
- Keep `environment_secrets.patternProperties` using the same regex.
- Preserve existing runtime validation for companion environment secret keys.
- Regenerate or update `openapi.json` consistently with the schema source.
- Run focused tests/checks only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a schema extra for `environment_secrets` that includes the same
   `propertyNames` key schema used by `environment`.
2. Add focused regression assertions for the Pydantic schema and checked-in
   OpenAPI artifact.
3. Regenerate `openapi.json` if the project generator is available; otherwise
   update the artifact minimally and document the focused checks.
4. Commit the thread-specific fix locally.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py -q`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`

Pass criteria: the focused tests pass, and the OpenAPI drift check reports no
diff. If dependency setup prevents execution, record the blocker in validation.

## Assumptions/Changes

- The OpenAPI generator is run through `uv --extra dev` in this workspace because
  bare `python` does not include FastAPI or the project dev dependencies.
