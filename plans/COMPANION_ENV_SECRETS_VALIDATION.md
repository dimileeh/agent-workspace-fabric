# Companion Environment Secrets Validation

## Result

Implemented issue #290: companion `environment` remains literal-only, and
`environment_secrets` now lets AWF inject env-backed companion secrets through
AWF-generated Compose placeholders without storing raw values in dispatch
payloads.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py -q`
  - `158 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py src/awf/node/companion_services.py src/awf/node/compose_manager.py src/awf/node/stack_launcher.py src/awf/node/provisioner.py tests/unit/api/test_schema_coverage_edges.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py`
  - passed
- `uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py src/awf/node/companion_services.py src/awf/node/compose_manager.py src/awf/node/stack_launcher.py src/awf/node/provisioner.py`
  - passed
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - passed

## Notes

- A first OpenAPI generation attempt with system `python` failed because the
  project dependencies were not loaded. Rerunning with `uv run --extra dev`
  succeeded and the final drift check passed.
- No raw secret values are included in companion service specs or metadata; the
  rendered compose file contains only AWF-generated placeholders.
