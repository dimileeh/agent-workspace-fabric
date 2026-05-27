# Companion YAML-Safe Paths Validation

Plan reference: `plans/COMPANION_YAML_SAFE_PATHS_PLAN.md`

## Requirement Status

- Reject public companion repo-relative path fields that could break raw
  double-quoted YAML rendering: Complete.
- Cover `build_context`, `dockerfile`, `env_file`, repo-relative volume
  sources, and volume targets rendered in the same raw compose scalar:
  Complete.
- Keep valid repo-relative and named-volume companion requests unchanged:
  Complete.
- Add focused regression tests before implementation: Complete.
- Run only targeted validation owned by this change: Complete.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`

Regression evidence:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  failed on the new YAML-safe path cases for `build_context`, `dockerfile`,
  `env_file`, and repo-relative volume source.

Final focused validation:

- `uv run --python 3.12 --extra dev ruff format tests/unit/api/test_schema_coverage_edges.py`
  reformatted the touched test file after the commit hook reported formatting
  drift.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  passed: 40 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  passed.

Full AWF/GitHub validation, full coverage gates, and CI-equivalent suites were
not run in this agent phase; AWF owns those after completion.
