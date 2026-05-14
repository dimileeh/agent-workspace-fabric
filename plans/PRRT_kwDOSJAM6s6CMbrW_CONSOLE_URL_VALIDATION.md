# PRRT_kwDOSJAM6s6CMbrW Console URL Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CMbrW_CONSOLE_URL_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for malformed configured console URLs via
  `collect_smoke_report()` and direct `_default_console_checker()` behavior in
  `tests/unit/service/test_smoke.py`.
- Complete: Updated `_default_console_checker()` in
  `src/awf/service/smoke.py` so `httpx.InvalidURL` returns `False`, matching
  existing unreachable console handling.
- Complete: Preserved existing reachable URL and HTTP status handling; the full
  smoke unit module passes.
- Complete: Ran the planned smoke unit verification.

## Evidence

- Failing pre-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py::TestCollectSmokeReportMockedMode::test_malformed_configured_console_url_reports_unavailable_warning -q`
  failed with `httpx.InvalidURL: Invalid port: 'badport'`.
- Focused post-fix regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py::TestCollectSmokeReportMockedMode::test_malformed_configured_console_url_reports_unavailable_warning tests/unit/service/test_smoke.py::TestCollectSmokeReportExceptionPaths::test_default_console_checker_treats_malformed_url_as_unreachable -q`
  passed.
- Smoke module:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py -q`
  passed with 45 tests.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/smoke.py tests/unit/service/test_smoke.py`
  passed.

## Files Changed

- `src/awf/service/smoke.py`
- `tests/unit/service/test_smoke.py`
- `plans/PRRT_kwDOSJAM6s6CMbrW_CONSOLE_URL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CMbrW_CONSOLE_URL_VALIDATION.md`
