# Provider Mapping Immutability Validation

Plan reference: `PROVIDER_MAPPING_IMMUTABILITY_PLAN.md`

## Requirement Status

- Add a regression proving provider mappings cannot be mutated in place after
  `HostSetupConfig` construction: Complete.
- Preserve existing credential-ref validation and secret payload rejection:
  Complete.
- Preserve YAML read/write round-trip behavior for valid host setup config:
  Complete.
- Keep the change localized to host setup config model and focused tests:
  Complete.
- Run only targeted tests or checks for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/PROVIDER_MAPPING_IMMUTABILITY_PLAN.md`
- `plans/PROVIDER_MAPPING_IMMUTABILITY_VALIDATION.md`

Focused checks:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_rejects_in_place_provider_mutation -q`
  failed before implementation because the mutable provider dict did not raise
  `TypeError`.
- Passing checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed with `15 passed`.
- Passing checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- Passing checks:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase because the workspace
contract assigns broad validation, provenance, logs, timeouts, and merge gating
to AWF and GitHub after agent completion.
