# Review 4460873446 Callback Key Cache Bound Validation

Plan reference:
`plans/REVIEW_4460873446_CALLBACK_KEY_CACHE_BOUND_PLAN.md`

## Requirement Status

- Complete: Added a regression proving the callback replay-key cache created
  for app state evicts older keys at the configured callback replay-key cache
  limit.
  - Evidence: `tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_app_state_is_bounded`
- Complete: Preserved the explicit unbounded cache default test.
  - Evidence: existing
    `tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_default_retains_keys_past_response_cache_limit`
    still passes in the callback API module.
- Complete: Avoided changing callback registration replay, conflict, and
  rate-limit behavior outside cache capacity.
  - Evidence: the callback API module passed.
- Complete: Kept the change scoped to callback route code, callback API tests,
  and plan/validation docs.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_app_state_is_bounded -q`
  - Before implementation: failed because the oldest key still matched after
    inserting one more entry than the configured limit.
  - After implementation: passed, `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Passed, `79 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/callbacks.py`
  - Passed.
- `git diff --check`
  - Passed.

## Gaps

None.
