# PRRT_kwDOSJAM6s6Fh8ee Expanded Resolve Fallback Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Fh8ee_EXPANDED_RESOLVE_FALLBACK_PLAN.md`

## Requirement Status

- Complete: Added a regression proving a `~`-prefixed candidate uses the
  expanded path when `resolve()` raises `OSError`.
- Complete: Preserved the existing fallback for `expanduser()` failures by
  keeping the original absolute candidate when no expanded path is available.
- Complete: Kept source-checkout failures reason-coded through
  `SourceCheckoutError` with `SOURCE_CHECKOUT_INVALID` and `path_status:
  missing`.
- Complete: Avoided broad AWF/GitHub-owned validation and ran only focused
  local checks.

## Evidence

- Changed `src/awf/host_setup/source_assets.py` so `_resolve_candidate`
  separates expansion and resolution fallbacks, returning `expanded.absolute()`
  when expansion succeeds but resolution fails.
- Added
  `tests/unit/service/test_host_setup_config.py::test_source_checkout_resolve_failure_uses_expanded_fallback`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "resolve_failure or expanduser_failure"`
  failed with diagnostics rooted at `/workspace/~/missing-awf-source-checkout`.
- Focused checks after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "resolve_failure or expanduser_failure"`
  passed with `2 passed, 37 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py`
  passed.

Full AWF/GitHub validation remains managed by AWF after agent completion.
