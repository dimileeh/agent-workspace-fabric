# Review 4454403868 Router Dependencies Validation

Plan reference:
`plans/REVIEW_4454403868_ROUTER_DEPENDENCIES_PLAN.md`

## Requirement Status

- Complete: Added a regression test in
  `tests/unit/api/test_openapi_artifact.py` proving a router-level guard whose
  resolved dependency graph includes `require_api_token` gets a required
  Authorization header in OpenAPI.
- Complete: Updated `_auth_required_operations` in `src/awf/api/app.py` to walk
  `APIRoute.dependant` recursively instead of checking only shallow route
  dependency declarations.
- Complete: Preserved existing route-level auth behavior and malformed-schema
  guard behavior by keeping the existing OpenAPI artifact test module green.
- Complete: Verified no checked-in OpenAPI drift is required.
- Complete: Prepared this change for a local review-comment-specific commit.

## Evidence

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_openapi_auth_contract_detects_resolved_router_level_auth_dependencies -q`
  failed because the generated Authorization header was `required: false`.
- Passing focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_openapi_auth_contract_detects_resolved_router_level_auth_dependencies -q`
  passed.
- Passing OpenAPI artifact suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passed with 15 tests.
- Passing lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/app.py tests/unit/api/test_openapi_artifact.py`
  passed.
- Passing format check after applying the repo formatter:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/api/app.py tests/unit/api/test_openapi_artifact.py`
  passed.
- Passing type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
- Passing spec drift check:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed and reported that `openapi.json` matches the current app spec.

## Notes

- `python scripts/generate_openapi.py --check` failed in this container because
  the base interpreter could not import `fastapi`. The equivalent command run
  through the repository's `uv` dev environment passed.
