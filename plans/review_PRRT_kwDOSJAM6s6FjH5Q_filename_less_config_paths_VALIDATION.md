# Review PRRT_kwDOSJAM6s6FjH5Q Filename-Less Config Paths Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6FjH5Q_filename_less_config_paths_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving filename-less config paths are
  reason-coded as `HOST_SETUP_CONFIG_WRITE_FAILED`.
- Complete: Preserved sanitized diagnostics by exposing only
  `{"error_type": "ValueError"}` and asserting the raw `Path.with_name()`
  message is absent.
- Complete: Kept normal atomic writes and existing write-failure behavior
  unchanged by only wrapping temporary-path construction failures.
- Complete: Ran focused host setup config checks only; full AWF/GitHub
  validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/review_PRRT_kwDOSJAM6s6FjH5Q_filename_less_config_paths_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6FjH5Q_filename_less_config_paths_VALIDATION.md`

Focused checks:

- Red state confirmed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_filename_less_paths_are_reason_coded -q`
  failed with raw `ValueError` from `Path.with_name()`.
- Green focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_filename_less_paths_are_reason_coded -q`
  passed, `3 passed`.
- Green touched test file:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed, `66 passed`.
- Green focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.

No partial or missing requirements remain.
