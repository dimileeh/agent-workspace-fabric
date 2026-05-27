# Companion Environment Keys Validation

Plan reference: `COMPANION_ENV_KEYS_PLAN.md`

## Requirement Status

- Add a regression test showing companion environment keys containing YAML
  structural characters are rejected: Complete.
- Constrain companion environment keys to Docker-style variable names before
  compose rendering: Complete.
- Keep the generated OpenAPI artifact aligned with the schema-level key
  restriction: Complete.
- Preserve valid companion environment values and existing companion validation
  behavior: Complete.
- Run only focused local checks; full AWF/GitHub validation remains managed
  after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`
- `plans/COMPANION_ENV_KEYS_PLAN.md`
- `plans/COMPANION_ENV_KEYS_VALIDATION.md`

Focused checks:

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  failed for the new `BAD:KEY` and `BAD\nKEY` cases before implementation.
- Artifact update:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
  regenerated `openapi.json`.
- Green checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_normalize_default_base_branch tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_workspace_companion_environment_keys_document_docker_names -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/api/test_openapi_artifact.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py`
  passed.

Full AWF/GitHub validation, including broad suite and merge gating, is left to
AWF after agent completion per the workspace contract.
