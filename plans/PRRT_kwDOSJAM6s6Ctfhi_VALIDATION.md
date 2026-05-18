# Validation: PRRT_kwDOSJAM6s6Ctfhi Legacy Flat Branch Default

Plan reference: `plans/PRRT_kwDOSJAM6s6Ctfhi_PLAN.md`

## Requirement Status

- Complete: Rich `WorkspaceRepo` requests that omit `base_branch` still default
  to `main`. Evidence: the updated schema regression test asserts the nested
  default remains `main`.
- Complete: Legacy flat `WorkspaceCreateRequest` payloads that omit
  `branch_base` default to `development`. Evidence: `_coerce_legacy_flat_payload`
  now uses `_LEGACY_FLAT_REPO_BASE_BRANCH_DEFAULT`.
- Complete: Legacy flat payloads that explicitly provide `branch_base` keep that
  provided value. Evidence: existing focused schema coverage for explicit
  legacy `branch_base` still passes.
- Complete: Regression coverage documents the compatibility split between rich
  and flat create payloads. Evidence:
  `test_legacy_flat_workspace_create_omitted_branch_base_preserves_development`.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  passed with 6 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/api/schemas.py tests/unit/api/test_schema_coverage_edges.py`
  passed.

## Gaps

None.
