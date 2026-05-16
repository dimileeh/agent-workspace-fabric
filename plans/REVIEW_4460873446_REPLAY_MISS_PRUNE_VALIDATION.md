# Review 4460873446 Replay Miss And Prune Validation

Plan reference:
`plans/REVIEW_4460873446_REPLAY_MISS_PRUNE_PLAN.md`

## Requirement Status

- Complete: Added a request-admission regression proving a prune scan records
  all bucket window sizes it evaluated and avoids an immediate second scan for
  another live window size.
- Complete: Preserved window-aware stale deletion for mixed window sizes.
- Complete: Added workspace v1/v2 regressions proving a known replay-key cache
  hit plus durable DB miss returns `409 IDEMPOTENCY_REPLAY_UNAVAILABLE` after
  one durable lookup and does not create a workspace.
- Complete: Added a callback regression proving the same known-key durable miss
  raises `409 IDEMPOTENCY_REPLAY_UNAVAILABLE`, does not retry durable replay,
  and does not register a subscription.
- Complete: Implemented the scoped limiter and route changes only.

## Evidence

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_limiter_marks_all_scanned_window_sizes_pruned tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_known_replay_key_db_miss_returns_conflict_without_create tests/unit/api/test_callbacks.py::test_register_callback_known_replay_key_db_miss_returns_conflict_without_register -q`
  - Before implementation: failed for all four new cases.
  - After implementation: passed, `4 passed in 0.94s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q`
  - Passed, `247 passed in 130.53s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py`
  - Initially found two explicit `return None` statements in test doubles.
  - After cleanup: passed.
- `uv run --python 3.12 --extra dev ruff format tests/unit/api/test_workspaces.py`
  - Applied formatting after the commit hook reported `tests/unit/api/test_workspaces.py`
    would be reformatted.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_known_replay_key_db_miss_returns_conflict_without_create -q`
  - Passed after formatting, `2 passed in 0.71s`.
- `uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py`
  - Passed.
- `git diff --check`
  - Passed.

## Remaining Gaps

None.
