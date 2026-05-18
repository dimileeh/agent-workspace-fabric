# Review Comment 4306428170 Validation

Plan reference: `plans/REVIEW_COMMENT_4306428170_PLAN.md`

## Requirement Status

- Complete: `WorkspaceValidation.commands` items reject `""`.
  - Evidence: `ValidationCommand = Annotated[str, Field(min_length=1)]` is used
    for `WorkspaceValidation.commands`, and
    `test_workspace_validation_rejects_empty_command_entries` covers the
    regression.
- Complete: ordinary non-empty command strings are still accepted.
  - Evidence: `test_workspace_validation_accepts_non_empty_command_entries`.
- Complete: generated `openapi.json` includes `minLength: 1` for command items.
  - Evidence: regenerated `openapi.json` and
    `test_workspace_validation_commands_are_non_empty_in_openapi`.
- Complete: narrow tests and drift check pass.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_validation_rejects_empty_command_entries tests/unit/api/test_openapi_artifact.py::test_workspace_validation_commands_are_non_empty_in_openapi -q
```

Expected TDD failure before the schema change: both tests failed because empty
commands were accepted and OpenAPI lacked `minLength`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_validation_rejects_empty_command_entries tests/unit/api/test_schema_coverage_edges.py::test_workspace_validation_accepts_non_empty_command_entries tests/unit/api/test_openapi_artifact.py::test_workspace_validation_commands_are_non_empty_in_openapi -q
```

Passed: 3 tests.

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py
uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py -q
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py
```

Passed: focused API tests, OpenAPI drift check, and lint.

## Notes

- The direct `python scripts/generate_openapi.py` command failed in this
  container because the base interpreter lacks `fastapi`; the same script
  passed through the repository `uv run --python 3.12 --extra dev` environment.
- No remaining gaps.
