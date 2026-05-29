# PRRT_kwDOSJAM6s6Fit9L Duplicate YAML Keys Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Fit9L_DUPLICATE_YAML_KEYS_PLAN.md`

## Requirement Status

- Complete: Added a regression proving a duplicate `credential_ref` key is
  rejected when an earlier value is a raw credential and a later value is a safe
  reference.
- Complete: Added a duplicate-key-rejecting `SafeLoader` path for
  `read_host_setup_config()` before the existing secret scan and Pydantic
  validation.
- Complete: Preserved safe diagnostics; duplicate keys report only
  `duplicate_mapping_key` and do not include raw credential values.
- Complete: Preserved existing non-duplicate host setup config behavior covered
  by the focused host setup config tests.
- Complete: Ran focused checks only. Full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/PRRT_kwDOSJAM6s6Fit9L_DUPLICATE_YAML_KEYS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Fit9L_DUPLICATE_YAML_KEYS_VALIDATION.md`

Focused checks:

- Failed before implementation as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "duplicate_yaml_key"`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "duplicate_yaml_key"`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "host_setup_config"`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`

No remaining planned gaps.
