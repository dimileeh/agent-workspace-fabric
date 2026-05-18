# Plan: PRRT_kwDOSJAM6s6Ctfhi Legacy Flat Branch Default

## Problem Statement And Scope

The review thread reports that the legacy flat `POST /v1/workspaces` compatibility
adapter now defaults omitted `branch_base` values to the rich contract default
`main`. Existing flat callers previously defaulted to `development`, and the MCP
compatibility path still uses that legacy default. This change should preserve
the rich nested schema default while restoring the legacy flat REST fallback.

## Requirements Checklist

- Rich `WorkspaceRepo` requests that omit `base_branch` still default to `main`.
- Legacy flat `WorkspaceCreateRequest` payloads that omit `branch_base` default to
  `development`.
- Legacy flat payloads that explicitly provide `branch_base` keep that provided
  value.
- Regression coverage documents the compatibility split between rich and flat
  create payloads.

## Implementation Steps

1. Update the schema regression test first so the currently wrong flat fallback
   fails against the existing implementation.
2. Add a named legacy flat branch default constant in `src/awf/api/schemas.py`.
3. Use that constant only in `_coerce_legacy_flat_payload`.
4. Run focused schema tests and lint for the changed Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py`
  passes.
