# Host Setup Config Copied Mapping Validation

Plan reference:
`plans/host_setup_config_copied_mapping_validation_PLAN.md`

## Requirement Status

- Add regression coverage for copied configs where `providers` is replaced by a
  non-mapping object: Complete.
- Add regression coverage for copied configs where `clients` is replaced by a
  non-mapping object: Complete.
- Preserve existing handling for invalid entries inside otherwise valid
  mappings: Complete.
- Ensure `write_host_setup_config()` raises sanitized `HostSetupConfigError`
  diagnostics before any YAML write for these copied invalid fields: Complete.
- Avoid broad AWF/GitHub-owned validation: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/host_setup_config_copied_mapping_validation_PLAN.md`
- `plans/host_setup_config_copied_mapping_validation_VALIDATION.md`

Focused checks:

- Initial failing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "model_copy_updates or copied_mapping"`
  - Result before implementation: 2 failed, 2 passed.
  - Failure showed `PydanticSerializationError` from `_serialize_providers`
    and `_serialize_clients`.
- Post-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "model_copy_updates or copied_mapping"`
  - Result: 4 passed, 66 deselected.
- Focused host setup config file:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Result: 70 passed.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  - Result: all checks passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  - Result: success, no issues found.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after agent completion.

## Gaps

None.
