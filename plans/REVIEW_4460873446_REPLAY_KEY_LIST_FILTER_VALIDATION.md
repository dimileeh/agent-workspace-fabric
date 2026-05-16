# Review 4460873446 Replay Key List Filter Validation

Plan reference:
`plans/REVIEW_4460873446_REPLAY_KEY_LIST_FILTER_PLAN.md`

## Requirement Status

- Complete: Added a regression proving the callback replay-key list query
  filters NULL idempotency keys and NULL request hashes.
  - Evidence:
    `tests/unit/db/test_callback_repository.py::test_subscription_repository_replay_key_list_filters_null_legacy_rows`
- Complete: Added the missing callback repository filters.
  - Evidence: `src/awf/db/repositories.py`
- Complete: Documented replay-key list helpers as bounded support hooks, not
  request-handler admission bypasses.
  - Evidence: `src/awf/db/repositories.py`,
    `src/awf/service/callbacks.py`
- Complete: Preserved route guards that fail if fresh rejected requests call
  the list helpers.
  - Evidence: focused callback/workspace API regressions passed.
- Complete: Ran focused validation and diff hygiene checks.
  - Evidence: commands below.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_replay_key_list_filters_null_legacy_rows -q`
  - Before implementation: failed because the emitted callback replay-key query
    did not include `IS NOT NULL` filters.
  - After implementation: passed, `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_with_lock_scoped_replay_check -q`
  - Passed, `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py -q`
  - Passed, `19 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/callbacks.py tests/unit/db/test_callback_repository.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `git diff --check`
  - Passed.

## Gaps

None.
