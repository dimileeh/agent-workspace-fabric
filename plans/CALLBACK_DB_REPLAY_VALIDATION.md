# Callback DB Replay Rate Limit Validation

Plan reference: `plans/CALLBACK_DB_REPLAY_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a persisted callback idempotency replay bypasses callback registration rate limiting when the route replay cache is cold.
- Complete: Updated the existing replay-read/rate-limit ordering test so it asserts actual `CallbackService.replay_existing()` calls before fresh-key rate-limit rejection.
- Complete: Wired `CallbackService.replay_existing()` into `register_callback()` between the in-memory replay-cache miss and the request admission gate.
- Complete: Preserved idempotency conflict handling by routing durable replay hash mismatches through the existing `409 IDEMPOTENCY_CONFLICT` helper.
- Complete: Preserved rate limiting for fresh callback registrations after the configured limit is exhausted.
- Complete: Removed the duplicated callback-route `_request_app_state()` helper by exporting and reusing `request_app_state()` from `src/awf/api/request_admission.py`.
- Complete: Kept changes scoped to callback registration/request-admission code, focused tests, and this plan/validation pair.

## Evidence

- Failing-first check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q -k "replay_bypasses_limit_when_replay_cache_is_cold or replay_miss"`
  - Failed before implementation because `replay_existing()` was not called and cold-cache replay returned `429`.
- Passing focused regression check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q -k "replay_bypasses_limit_when_replay_cache_is_cold or replay_miss"`
  - `2 passed, 54 deselected`
- Passing callback API surface:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - `56 passed`
- Passing request-admission coverage:
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
  - `26 passed`
- Passing lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/api/request_admission.py tests/unit/api/test_callbacks.py`
  - `All checks passed!`
- Passing type check:
  - `uv run --python 3.12 --extra dev mypy src/awf/api/routes/callbacks.py src/awf/api/request_admission.py`
  - `Success: no issues found in 2 source files`

## Gaps

None.
