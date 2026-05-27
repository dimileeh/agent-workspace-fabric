# Companion Environment Keys Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6FL-zG` reports that public companion `environment`
keys are accepted as arbitrary strings and later rendered as unquoted Compose
YAML keys. The fix should reject unsafe keys at the API schema boundary while
preserving valid companion environment maps.

## Requirements Checklist

- Add a regression test showing companion environment keys containing YAML
  structural characters are rejected.
- Constrain companion environment keys to Docker-style variable names before
  compose rendering.
- Keep the generated OpenAPI artifact aligned with the schema-level key
  restriction.
- Preserve valid companion environment values and existing companion validation
  behavior.
- Run only focused local checks; full AWF/GitHub validation remains managed
  after agent completion.

## Implementation Steps

1. Add a focused failing unit test in the existing companion schema edge tests.
2. Add schema-level validation for companion environment map keys.
3. Update the OpenAPI artifact if the generated schema changes.
4. Run the focused unit tests that cover the new behavior and schema docs.
5. Record validation evidence in `COMPANION_ENV_KEYS_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_normalize_default_base_branch tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_workspace_companion_environment_keys_document_docker_names -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py`
  passes.

Full AWF/GitHub validation is intentionally not run during the agent phase.
