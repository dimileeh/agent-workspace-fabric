# PRRT_kwDOSJAM6s6Ce2hx Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Ce2hx_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving fresh over-limit callbacks do not
  call the all-key durable replay warmup.
  - Evidence: `tests/unit/api/test_callbacks.py`
  - Failed before implementation with `AssertionError: fresh over-limit callbacks
    must not scan all replay keys`.
- Complete: Replaced all-key warmup on the rejected callback path with a bounded
  lookup for only the submitted idempotency key.
  - Evidence: `src/awf/api/routes/callbacks.py`,
    `src/awf/service/callbacks.py`, `src/awf/db/repositories.py`
- Complete: Preserved existing idempotent replay, conflict, and rate-limit
  behavior.
  - Evidence: callback API suite passed.
- Complete: Kept changes scoped to callback route/service/repository support and
  tests.
  - Evidence: changed files are limited to callback implementation/tests and
    plan/validation docs.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss -q`
  - Failed before implementation as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold tests/unit/api/test_callbacks.py::test_register_callback_same_key_with_changed_body_returns_conflict -q`
  - Passed: 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_gets_idempotency_request_hash_by_key tests/unit/service/test_callbacks.py::test_callback_service_gets_idempotency_request_hash_by_key -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Passed: 78 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py tests/unit/service/test_callbacks.py -q`
  - Passed: 69 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
