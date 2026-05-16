# Request Admission Review Fix Validation

Plan reference: `plans/REQUEST_ADMISSION_REVIEW_FIX_PLAN.md`

## Requirement Status

- Document fixed-window burst-boundary trade-off: Complete.
  - Evidence: `src/awf/api/request_admission.py` class docstring now describes
    the boundary burst behavior and sliding-window trade-off.
- Clarify private prune helper lock contract: Complete.
  - Evidence: `RequestAdmissionLimiter._prune()` now documents that callers
    must not already hold `self._lock`.
- Fail loudly for real callback requests without app state: Complete.
  - Evidence: `src/awf/api/routes/callbacks.py` raises `RuntimeError` for real
    Starlette `Request` objects without `request.app.state` in both callback
    replay cache helpers.
- Preserve direct fallback behavior: Complete.
  - Evidence: existing tests for `SimpleNamespace()` and `None` direct callers
    continue to pass.
- Add focused regression tests: Complete.
  - Evidence: new tests in `tests/unit/api/test_callbacks.py` cover both real
    request failure paths.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q -k 'real_request_without_app_state_fails_loudly'`
  - Before implementation: failed with both helpers not raising `RuntimeError`.
  - After implementation: passed, `2 passed, 73 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_deps.py -q`
  - Passed: `105 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`
  - Passed.

## Gaps

None.
