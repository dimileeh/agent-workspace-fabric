# PRRT_kwDOSJAM6s6Ffq3m Source Marker OSError Validation

Plan reference: `PRRT_kwDOSJAM6s6Ffq3m_SOURCE_MARKER_OSERROR_PLAN.md`

## Requirement Status

- Complete: Actual missing markers still populate `SourceCheckoutError.missing_markers`.
- Complete: Marker probe `OSError` failures populate `details["unreadable_paths"]`.
- Complete: Marker probe `OSError` failures no longer also populate `missing_markers`.
- Complete: Existing source checkout validation behavior in the focused test file remains green.

## Evidence

Files changed:

- `src/awf/host_setup/source_assets.py`
- `tests/unit/service/test_host_setup_config.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Passed: 13 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  - Passed.

Full AWF/GitHub validation is managed by AWF after agent completion per the workspace contract.
