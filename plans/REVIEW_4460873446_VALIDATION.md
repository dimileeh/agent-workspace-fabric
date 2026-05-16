# Review Comment 4460873446 Validation

Plan reference: `plans/REVIEW_4460873446_PLAN.md`

## Requirement Status

- **Complete** — Add/update failing tests before implementation where behavior changes.
  - Added callback API and repository regressions first.
  - Confirmed pre-fix failures with:
    `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limited_replay_reads_durable_hash_without_advisory_lock tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit -q`
  - Evidence: 3 expected failures before implementation.

- **Complete** — `CallbackService.get_idempotency_request_hash()` performs only the request-hash read and does not call `acquire_idempotency_key_lock()`.
  - Evidence: `src/awf/service/callbacks.py`
  - Regression: `tests/unit/api/test_callbacks.py::test_register_callback_rate_limited_replay_reads_durable_hash_without_advisory_lock`

- **Complete** — `WorkspaceRepository.list_idempotency_replay_keys()` is bounded by default and supports explicit smaller limits.
  - Evidence: `src/awf/db/repositories.py`
  - Regression: `tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded`

- **Complete** — `CallbackSubscriptionRepository.list_idempotency_replay_keys()` is bounded by default and supports explicit smaller limits.
  - Evidence: `src/awf/db/repositories.py`
  - Regression: `tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit`

- **Complete** — Callback durable replay no longer contains the dead `if not known_replay_key` branch after `remember_hash()`.
  - Evidence: `src/awf/api/routes/callbacks.py`

- **Complete** — Existing create/replay locking semantics are preserved for write/full replay decisions.
  - Evidence: callback API file pass and existing `test_callback_registration_locks_idempotency_key_before_lookup` remains green.

- **Complete** — Reason-catalog validation cleanup for rate-limit/idempotency error codes.
  - Evidence: `src/awf/service/doctor/reasons.py` and generated `docs/REASON_CATALOG.md`
  - Regressions:
    `tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage`
    `tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source`

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limited_replay_reads_durable_hash_without_advisory_lock tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit -q`
  - Result before implementation: failed as expected.
  - Result after implementation: `3 passed in 3.03s`.

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/db/test_callback_repository.py tests/unit/db/test_workspace_repository.py::TestIdempotency -q`
  - Result: `104 passed in 44.77s`.

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limited_replay_reads_durable_hash_without_advisory_lock tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source -q`
  - Result: `5 passed in 2.60s`.

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Result: `82 passed in 29.19s`.

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_uses_verified_bearer_identity_for_rate_limit -q`
  - Result: `1 passed in 3.72s`.

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source -q`
  - Result: `2 passed in 1.10s`.

- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed.

- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

- `git diff --check`
  - Result: passed.

## Full Unit Attempts

- Attempt 1: `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Result: `6589 passed`, `1 failed`.
  - Failure: `tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage` missing `CALLBACK_REGISTER_RATE_LIMITED`, `IDEMPOTENCY_REPLAY_UNAVAILABLE`, and `WORKSPACE_CREATE_RATE_LIMITED`.
  - Iteration: added generated reason text/source entries.

- Attempt 2: `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Result: `6589 passed`, `1 failed`.
  - Failure: `tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source`.
  - Iteration: moved the entries into `src/awf/service/doctor/reasons.py` and regenerated `docs/REASON_CATALOG.md`.

- Attempt 3: `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Result: `6589 passed`, `1 failed`.
  - Failure: `tests/unit/api/test_callbacks.py::test_register_callback_uses_verified_bearer_identity_for_rate_limit` returned `201` for the second request instead of `429`.
  - Follow-up evidence: the failed test passes in isolation, and the full `tests/unit/api/test_callbacks.py` file passes. No code in this review fix changes the fresh-key admission path exercised by that test.

## Iterations On Gaps

- **Iteration 1** — Full unit exposed undocumented rate-limit/idempotency error codes.
  - Action: documented the missing codes.
  - Result: catalog coverage then exposed that the docs are generated from doctor reason source.

- **Iteration 2** — Generated reason catalog source was missing the same codes.
  - Action: added `_REASON_TEXT` entries and regenerated `docs/REASON_CATALOG.md`.
  - Result: both catalog coverage and generated-doc sync tests passed.

- **Iteration 3** — Final full unit pass exposed an order-dependent callback rate-limit failure.
  - Action: reran the exact failed test and the full callback API test file.
  - Result: both passed; no further code change was made because the failure did not reproduce in the touched test surface or isolated target.

## Remaining Gaps

- **Partial** — A completely green full `tests/unit` pass was not obtained in this workspace because the final run hit an order-dependent callback rate-limit failure that passes in isolation and in the callback API file. The review-fix regressions, affected files, reason-catalog guards, lint, typecheck, and diff hygiene all pass.
