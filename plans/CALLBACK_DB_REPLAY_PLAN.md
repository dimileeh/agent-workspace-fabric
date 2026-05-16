# Callback DB Replay Rate Limit Plan

## Problem Statement And Scope

PR review comment `issue:4460873446` reports that `POST /v1/callbacks` only checks the in-memory idempotency replay cache before applying request admission. A persisted callback registration replay can therefore be rejected with `429` after process restart, across app instances, or whenever the route cache is cold. This violates the AWF mutating API idempotency contract.

Scope is limited to callback registration replay ordering, the related regression tests, and removing duplicated app-state helper code from the callback route.

## Requirements Checklist

- [ ] Add a regression test proving a persisted callback idempotency replay bypasses callback registration rate limiting when the route replay cache is cold.
- [ ] Update the existing replay-read/rate-limit ordering test so it no longer passes vacuously and proves DB replay lookup occurs before rate-limit rejection for fresh keys.
- [ ] Wire `CallbackService.replay_existing()` into `register_callback()` between the in-memory replay-cache miss and the request admission gate.
- [ ] Preserve conflict behavior for reused idempotency keys with changed callback payloads.
- [ ] Preserve rate limiting for fresh callback registrations after the configured limit is exhausted.
- [ ] Remove the duplicated `_request_app_state()` helper in `src/awf/api/routes/callbacks.py` by reusing the request-admission helper.
- [ ] Keep changes scoped and commit locally on the existing AWF branch.

## Implementation Steps

1. Update callback API tests first: add the cold replay-cache regression, and make the fresh-key ordering test assert actual `replay_existing()` calls.
2. Run the focused tests to confirm the new/updated regression fails on the current implementation.
3. Update `src/awf/api/request_admission.py` to expose the app-state helper for shared route use.
4. Update `src/awf/api/routes/callbacks.py` to create the service before admission, call `replay_existing()` after cache miss, return/cache durable replays, and use the shared app-state helper.
5. Run focused callback tests, then targeted lint/type checks for touched Python files where practical.
6. Create `plans/CALLBACK_DB_REPLAY_VALIDATION.md` with requirement status and command evidence.
7. Stage only changed files and commit with the required review-comment message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Passes with the cold-cache replay and ordering regressions covered.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/api/request_admission.py tests/unit/api/test_callbacks.py`
  - No lint findings.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/callbacks.py src/awf/api/request_admission.py`
  - No type errors for touched API modules.
