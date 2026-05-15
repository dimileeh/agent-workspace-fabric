# Review Issue 4460873446 Validation

Plan reference: `plans/review_issue_4460873446_PLAN.md`

## Requirement Status

- Complete: Remove or avoid process-global fallback state accumulation for direct-call request admission and callback replay-cache paths that lack app state.
  - Evidence: `src/awf/api/request_admission.py` now returns a fresh limiter for `request=None` and request-object-local limiter state for request-like objects without app state. `src/awf/api/routes/callbacks.py` does the same for direct callback replay caches.
- Complete: Preserve normal FastAPI app-state scoped limiter/cache behavior.
  - Evidence: Existing app-state lookup paths remain unchanged; broad API tests passed.
- Complete: Move callback replay-cache LRU promotion so conflicting payloads do not refresh eviction priority.
  - Evidence: `src/awf/api/routes/callbacks.py` now calls `move_to_end()` only after the request hash comparison succeeds.
- Complete: Add regression coverage for conflict non-promotion.
  - Evidence: `tests/unit/api/test_callbacks.py::test_callback_replay_conflict_does_not_promote_lru_entry`.
- Complete: Add regression coverage proving `request=None` admission calls do not accumulate shared quota across tests/direct calls.
  - Evidence: `tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter`.
- Complete: Confirm the shared v1/v2 workspace-create bucket as intentional with focused coverage and configuration documentation.
  - Evidence: `tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_and_v2_create_share_workspace_create_rate_limit_bucket` and updated `workspace_create_rate_limit_count` description in `src/awf/common/config.py`.
- Complete: Run the narrow tests and lint/type checks needed for the touched area.
  - Evidence: commands below.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter tests/unit/api/test_callbacks.py::test_callback_replay_cache_without_app_state_is_request_local tests/unit/api/test_callbacks.py::test_callback_replay_conflict_does_not_promote_lru_entry tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_and_v2_create_share_workspace_create_rate_limit_bucket -q`
  - Initial TDD result before implementation: failed for the three newly covered defects and passed for the shared v1/v2 bucket documentation test.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_none_request_uses_fresh_direct_limiter tests/unit/api/test_deps.py::test_request_admission_reuses_limiter_without_app_state tests/unit/api/test_callbacks.py::test_callback_replay_cache_without_app_state_is_request_local tests/unit/api/test_callbacks.py::test_callback_replay_conflict_does_not_promote_lru_entry tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_and_v2_create_share_workspace_create_rate_limit_bucket -q`
  - Result: `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces.py -q`
  - Result: `225 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Remaining Gaps

None.
