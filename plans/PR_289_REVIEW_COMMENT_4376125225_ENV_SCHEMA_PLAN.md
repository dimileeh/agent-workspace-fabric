# Plan: Address PR #289 review comment 4376125225

## Problem Statement and Scope

CodeRabbit review comment `4376125225` notes that
`WorkspaceCompanionRequest.environment` rejects Docker Compose interpolation in
values at validation time, but the public JSON Schema only exposes key
constraints. Scope is limited to the companion request schema, checked-in
OpenAPI artifact, a focused schema contract test, and this plan/validation
evidence.

## Requirements Checklist

- [ ] Verify the review finding is still valid against current code.
- [ ] Add focused regression coverage that fails while the public schema omits
      the value interpolation rule.
- [ ] Update the public schema metadata for `environment` values to document and
      express the Compose interpolation rejection without weakening existing key
      constraints.
- [ ] Regenerate the checked-in OpenAPI artifact so the public API document does
      not drift from the updated schema.
- [ ] Run focused validation only; leave broad AWF/GitHub validation to the
      post-agent pipeline.
- [ ] Commit only the files changed for review comment `4376125225`.

## Implementation Steps

1. Inspect `src/awf/api/schemas_companions.py` and existing companion schema
   tests.
2. Add a narrow test asserting that the generated environment schema includes a
   description and a value-level `not.pattern` rule.
3. Run the new test to confirm it fails against the current schema.
4. Extend the environment field schema extra and description.
5. Regenerate `openapi.json`.
6. Re-run the focused companion schema tests, OpenAPI drift check, and focused
   lint for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q -k companion_environment_schema`
  - Fails before implementation because the schema omits the value rule.
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q -k workspace_companions`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passes after regenerating `openapi.json`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Passes after implementation.

Full repository validation, coverage gates, frontend builds, push, and PR
updates are owned by AWF/GitHub after agent completion.
