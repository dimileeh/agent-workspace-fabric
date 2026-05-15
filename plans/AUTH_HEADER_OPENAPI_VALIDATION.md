# Authorization Header OpenAPI Validation

Plan reference: `plans/AUTH_HEADER_OPENAPI_PLAN.md`

## Requirement Status

- Add a regression test proving every required `authorization` header schema is
  a non-null string: Complete.
  - Evidence: `tests/unit/api/test_openapi_artifact.py` adds
    `test_required_authorization_headers_are_non_nullable_strings_in_openapi`.
  - Red evidence: the focused test failed before implementation with 18 invalid
    required authorization headers.
- Update OpenAPI generation so auth-required `authorization` headers are marked
  required and documented as `type: string` with `minLength: 1`: Complete.
  - Evidence: `src/awf/api/app.py` now normalizes the schema in
    `_mark_authorization_header_parameters_required`.
- Regenerate `openapi.json` from the app rather than hand-editing the artifact:
  Complete.
  - Evidence: `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
    wrote the checked-in artifact.
- Validate with the narrow OpenAPI tests and the OpenAPI drift check: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
    passed with 11 tests.
  - Evidence: `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
    reported the checked-in artifact matches the generated spec.
- Commit only the files changed for this thread with a conventional commit
  referencing `PRRT_kwDOSJAM6s6CNJ4D`: Complete after commit.

## Additional Verification

- `uv run --python 3.12 --extra dev ruff check src/awf/api/app.py tests/unit/api/test_openapi_artifact.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
