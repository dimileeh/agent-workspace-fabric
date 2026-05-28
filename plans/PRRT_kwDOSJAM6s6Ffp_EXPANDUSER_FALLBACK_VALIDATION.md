# PRRT_kwDOSJAM6s6Ffp Expanduser Fallback Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Ffp_EXPANDUSER_FALLBACK_PLAN.md`

## Requirement Status

- Complete: Added a regression proving an `expanduser()` `RuntimeError` falls
  back into reason-coded source-checkout validation instead of escaping.
- Complete: Preserved the existing `resolve()` `OSError` fallback behavior by
  keeping `resolve()` inside the same fallback block.
- Complete: Kept diagnostics flowing through `SourceCheckoutError` with
  `SOURCE_CHECKOUT_INVALID` and `path_status: missing`.
- Complete: Avoided broad AWF/GitHub-owned validation and ran only focused
  local checks.

## Evidence

- Changed `src/awf/host_setup/source_assets.py` so `_resolve_candidate` builds
  `base = Path(root)`, performs `base.expanduser().resolve()` inside the
  `try`, and catches `RuntimeError` with `OSError` before returning
  `base.absolute()`.
- Added
  `tests/unit/service/test_host_setup_config.py::test_source_checkout_expanduser_failure_remains_reason_coded`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k expanduser`
  failed with `RuntimeError: Could not determine home directory.`
- Focused checks after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "expanduser or valid_source_checkout or invalid_source_checkout or stale_source_checkout or unreadable_source_checkout"`
  passed with `5 passed, 7 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  passed.

Full AWF/GitHub validation remains managed by AWF after agent completion.
