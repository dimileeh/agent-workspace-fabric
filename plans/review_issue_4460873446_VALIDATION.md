# Review Issue 4460873446 Validation

Plan reference: `plans/review_issue_4460873446_PLAN.md`

## Requirement Status

- Complete: Add a regression proving real `Request` objects without app state fail loudly instead of using a request-local limiter.
  - Evidence: `tests/unit/api/test_deps.py::test_request_admission_real_request_without_app_state_fails_loudly`.
- Complete: Preserve direct-call compatibility for `None` and non-Starlette test objects.
  - Evidence: existing `tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter` and `tests/unit/api/test_deps.py::test_request_admission_reuses_limiter_without_app_state` still pass.
- Complete: Add a regression proving verified-bearer downgrade emits a structured warning without exposing raw tokens.
  - Evidence: `tests/unit/api/test_deps.py::test_request_admission_logs_verified_bearer_header_downgrade`.
- Complete: Implement the smallest request-admission change that satisfies the regressions.
  - Evidence: `src/awf/api/request_admission.py` now raises for real Starlette requests missing `request.app.state` and logs the verified-bearer-to-client-host downgrade with endpoint family and fallback identity type only.
- Complete: Run the focused unit tests for the touched area.
  - Evidence: commands below.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q -k 'request_admission_logs_verified_bearer_header_downgrade or request_admission_real_request_without_app_state_fails_loudly'`
  - Initial TDD result before implementation: failed for both new regressions.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
  - Result: `30 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py tests/unit/api/test_deps.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py`
  - Result: `Success: no issues found in 1 source file`.
- `git diff --check`
  - Result: passed.

## Remaining Gaps

None.
