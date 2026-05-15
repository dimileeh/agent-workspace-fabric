# Request Admission Limiter Lock Validation

Plan reference: `REQUEST_ADMISSION_LIMITER_LOCK_PLAN.md`

## Requirement Status

- Add a regression test showing concurrent admissions for the same identity do
  not exceed the configured limit: Complete.
  - Evidence: Added
    `test_request_admission_limiter_serializes_concurrent_admissions` in
    `tests/unit/api/test_deps.py`.
  - TDD evidence: The focused test failed before the implementation with all 8
    concurrent calls allowed against a limit of 1.
- Guard all reads and mutations of limiter bucket/prune state with a lock:
  Complete.
  - Evidence: `RequestAdmissionLimiter` now owns a lock; `admit` serializes
    bucket reads/writes and pruning, and `_prune` delegates to a lock-protected
    helper.
- Preserve existing fixed-window behavior, retry metadata, and pruning behavior:
  Complete.
  - Evidence: Existing request-admission tests pass unchanged.
- Keep the change local to request admission and its tests: Complete.
  - Evidence: Code changes are limited to
    `src/awf/api/request_admission.py` and `tests/unit/api/test_deps.py`, plus
    required plan/validation documents.
- Validate with the narrow unit tests and static checks appropriate to the
  touched files: Complete.
  - Evidence:
    - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
      passed with 27 tests.
    - `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py tests/unit/api/test_deps.py`
      passed.
    - `uv run --python 3.12 --extra dev mypy src/awf` passed.
- Commit the fix locally without pushing: Complete.
  - Evidence: Local commit for this fix cycle uses the review-thread id in its
    conventional commit message. No push was performed.

## Gaps

No implementation or validation gaps remain.
