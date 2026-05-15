# Callback Durable Replay Quota Validation

Plan reference: `PRRT_kwDOSJAM6s6Cfh_A_CALLBACK_REPLAY_QUOTA_PLAN.md`

## Requirement Status

- Complete: Added `test_register_callback_cold_db_replay_does_not_spend_fresh_quota`, which creates one fresh callback, clears both replay caches, replays the persisted key, admits a second fresh callback under a limit of 2, and rate-limits only the third fresh callback.
- Complete: Preserved the fresh over-limit guard that avoids all-key replay scans; `test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss` still passes.
- Complete: Preserved known replay-key and durable replay behavior by routing persisted-key matches through the existing conflict and replay-unavailable helper path.
- Complete: Kept implementation scoped to callback registration admission/replay ordering and callback API tests.
- Complete: Ran the focused callback API validation surface.

## Evidence

- Initial red test: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_cold_db_replay_does_not_spend_fresh_quota -q` failed because `second_fresh.status_code` was `429` instead of `201`.
- Passing focused set: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_cold_db_replay_does_not_spend_fresh_quota tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold tests/unit/api/test_callbacks.py::test_register_callback_rate_limited_replay_locks_before_durable_lookup -q` passed with 4 tests.
- Passing callback API suite: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q` passed with 82 tests.
- Lint: `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py` passed.

## Files Changed

- `src/awf/api/routes/callbacks.py`
- `tests/unit/api/test_callbacks.py`
- `plans/PRRT_kwDOSJAM6s6Cfh_A_CALLBACK_REPLAY_QUOTA_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Cfh_A_CALLBACK_REPLAY_QUOTA_VALIDATION.md`
