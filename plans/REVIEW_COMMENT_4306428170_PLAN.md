# Review Comment 4306428170 Plan

## Problem Statement

CodeRabbit flagged that `WorkspaceValidation.commands` in `openapi.json`
accepts empty strings. The public workspace-create contract should reject empty
validation command entries at the schema level.

## Scope

- Update the source API schema so generated OpenAPI marks validation commands
  as non-empty strings.
- Regenerate `openapi.json` from the app schema instead of editing it by hand.
- Add focused regressions proving the model rejects empty commands and OpenAPI
  advertises `minLength: 1`.

## Requirements Checklist

- [x] `WorkspaceValidation.commands` items reject `""`.
- [x] `WorkspaceValidation.commands` items are still ordinary string commands.
- [x] Generated `openapi.json` includes `minLength: 1` for command items.
- [x] Narrow tests and OpenAPI drift check pass.

## Implementation Steps

1. Add failing tests for `WorkspaceValidation` model validation and OpenAPI
   item schema.
2. Introduce a reusable non-empty validation command string type in
   `src/awf/api/schemas.py`.
3. Regenerate `openapi.json`.
4. Run focused tests and `python scripts/generate_openapi.py --check`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py -q
python scripts/generate_openapi.py --check
```
