# Revalidate Copied Host Setup Config Validation

Plan reference: `PRRT_kwDOSJAM6s6Fi4p0_REVALIDATE_COPIED_CONFIG_PLAN.md`

## Requirement Status

- Add a regression proving a `model_copy(update=...)` config with an invalid
  provider credential reference is rejected before any file is written:
  Complete.
- Re-run `HostSetupConfig` validation on the dumped write payload before YAML
  serialization reaches disk: Complete.
- Preserve existing secret-payload write errors and sanitized diagnostics:
  Complete.
- Keep the change localized to host setup config code, focused tests, and
  required plan/validation docs: Complete.
- Run only targeted tests or checks for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/PRRT_kwDOSJAM6s6Fi4p0_REVALIDATE_COPIED_CONFIG_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Fi4p0_REVALIDATE_COPIED_CONFIG_VALIDATION.md`

Focused checks:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_revalidates_model_copy_updates -q`
  failed before implementation because `write_host_setup_config()` did not raise
  `HostSetupConfigError`.
- Passing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_revalidates_model_copy_updates -q`
  passed.
- Passing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed with `60 passed`.
- Passing check:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- Passing check:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase because the workspace
contract assigns broad validation, provenance, logs, timeouts, and merge gating
to AWF and GitHub after agent completion.
