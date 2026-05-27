# PRRT_kwDOSJAM6s6FOyJn Companion Healthcheck Interpolation Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FOyJn_COMPANION_HEALTHCHECK_INTERPOLATION_PLAN.md`

## Requirement Status

- Complete: Reject Docker Compose interpolation syntax in public companion `healthcheck_cmd` values.
- Complete: Preserve existing accepted companion healthcheck commands that do not contain interpolation syntax.
- Complete: Add a regression test for the review-reported secret interpolation shape.
- Complete: Keep the change scoped to companion API schema validation and its focused tests.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Failed because the `${GITHUB_TOKEN}` healthcheck command did not raise `ValidationError`.

Verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Passed: `20 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_normalize_default_base_branch -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  - Passed: `42 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Passed.

Full AWF/GitHub validation is managed by AWF after agent completion per the workspace contract.

## Gaps

None.
