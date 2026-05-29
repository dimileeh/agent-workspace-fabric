# Revalidate Host Setup Config Before Serialization Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6FjM9e` reports that
`write_host_setup_config()` still calls `model_dump(mode="json")` before it
revalidates copied or constructed `HostSetupConfig` instances. Invalid values
that are not JSON serializable, such as `providers={"github": object()}`, can
therefore raise raw Pydantic serialization errors instead of the sanitized
`HostSetupConfigError` contract.

Scope is limited to host setup config write-time validation and focused
regression coverage for copied configs containing non-serializable invalid
values. Broad AWF/GitHub validation remains owned by AWF after this agent
cycle.

## Requirements Checklist

- Add a regression proving a copied config with a non-serializable invalid
  provider value raises `HostSetupConfigError` before any file is written.
- Revalidate the config from a Python-mode payload before JSON serialization.
- Preserve existing sanitized secret-payload and validation diagnostics.
- Keep changes localized to host setup config code, focused tests, and required
  plan/validation docs.
- Run only targeted checks for the changed behavior.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_host_setup_config.py`
   for `model_copy(update={"providers": {"github": object()}})`.
2. Update `write_host_setup_config()` to build a Python-mode payload, scan it
   for secrets, validate it as `HostSetupConfig`, then JSON-dump only the
   validated model.
3. Preserve existing reason-code mapping for secret and corrupt config errors.
4. Run the new regression and relevant focused host setup config tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_wraps_non_serializable_model_copy_updates -q`
  - Fails before implementation with the raw serialization error and passes
    after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_revalidates_model_copy_updates tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_wraps_non_serializable_model_copy_updates tests/unit/service/test_host_setup_config.py::test_host_setup_config_rejects_secret_like_mapping_keys -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  - Passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
