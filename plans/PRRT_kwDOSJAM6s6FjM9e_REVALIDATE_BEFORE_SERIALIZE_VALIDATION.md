# Revalidate Host Setup Config Before Serialization Validation

Plan reference: `PRRT_kwDOSJAM6s6FjM9e_REVALIDATE_BEFORE_SERIALIZE_PLAN.md`

## Requirement Status

- Add a regression proving a copied config with a non-serializable invalid
  provider value raises `HostSetupConfigError` before any file is written:
  Complete.
- Revalidate the config from a Python-mode payload before JSON serialization:
  Complete.
- Preserve existing sanitized secret-payload and validation diagnostics:
  Complete.
- Keep changes localized to host setup config code, focused tests, and required
  plan/validation docs: Complete.
- Run only targeted checks for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/PRRT_kwDOSJAM6s6FjM9e_REVALIDATE_BEFORE_SERIALIZE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FjM9e_REVALIDATE_BEFORE_SERIALIZE_VALIDATION.md`

Focused checks:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_wraps_non_serializable_model_copy_updates -q`
  failed before implementation with
  `PydanticSerializationError: Unable to serialize unknown type: <class 'object'>`.
- Passing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_wraps_non_serializable_model_copy_updates tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_revalidates_model_copy_updates tests/unit/service/test_host_setup_config.py::test_host_setup_config_rejects_secret_like_mapping_keys -q`
  passed with `3 passed`.
- Passing check:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- Passing check:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase because the workspace
contract assigns broad validation, provenance, logs, timeouts, and merge gating
to AWF and GitHub after agent completion.
