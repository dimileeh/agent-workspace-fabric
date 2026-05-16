# Request Admission Review Fix Plan

## Problem Statement and Scope

Address PR #256 review-level feedback for request admission limiting and callback
idempotency replay cache behavior. Keep the change limited to the rate-limiter
documentation and the callback cache helper contract for real Starlette
requests without attached app state.

## Requirements Checklist

- Document the fixed-window burst-boundary trade-off in
  `RequestAdmissionLimiter`.
- Clarify the private prune helper lock contract so future callers do not call
  it while holding the limiter lock.
- Align callback idempotency replay cache helpers with
  `request_admission_limiter`: real Starlette `Request` objects without
  `request.app.state` must fail loudly instead of silently using a direct cache.
- Preserve existing direct fallback behavior for `None` and non-Starlette test
  objects.
- Add focused regression tests for the callback helper behavior change.

## Implementation Steps

1. Add regression tests in `tests/unit/api/test_callbacks.py` for real
   Starlette requests without app state.
2. Update `src/awf/api/routes/callbacks.py` to raise `RuntimeError` for real
   requests missing app state in both replay cache helpers.
3. Update `src/awf/api/request_admission.py` docstrings for fixed-window
   boundary behavior and private prune locking.
4. Run the narrow callback/deps unit tests and lint the touched Python files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_deps.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`

Pass criteria: commands exit 0 and the new tests fail before the implementation
change would be applied.
