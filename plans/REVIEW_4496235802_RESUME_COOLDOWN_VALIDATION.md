# Review 4496235802 Resume Cooldown Validation

Plan reference:
`plans/REVIEW_4496235802_RESUME_COOLDOWN_PLAN.md`

## Requirement Status

- Preserve the current protection for `VALIDATION_REQUESTED` salvage when the
  current worker has no executor: Complete. The current branch already writes
  `workspace.active_execution_salvage_blocked` with
  `validation_executor_unavailable`, and the targeted regression still passes.
- Add a bounded cache for active salvage monitor resume cooldowns: Complete.
  Added `_ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT` and routed cooldown
  writes through a bounded helper.
- Evict expired cooldown entries even when the expired workspace is not the one
  currently being checked or inserted: Complete. The eviction helper scans the
  session-local cooldown cache and removes expired entries on insert/check.
- Evict oldest live cooldown entries when the cache exceeds its limit:
  Complete. The helper preserves insertion order and removes oldest entries
  until the cache is within the configured bound.
- Preserve existing cooldown semantics for live entries: Complete. Live
  cooldown checks still return `True` until `monotonic()` reaches the stored
  deadline.
- Add a regression proving expired entries are pruned and oldest live entries
  are evicted over the limit: Complete.
- Run the narrow affected unit tests and lint for touched Python files:
  Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4496235802_RESUME_COOLDOWN_PLAN.md`
- `plans/REVIEW_4496235802_RESUME_COOLDOWN_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted'`
  - Failed before the production change with missing bounded cooldown support.
  - Passed after implementation: 1 passed, 263 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'active_salvage_monitor_recovery_operation_ids_are_bounded or active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted or preserved_active_validation_salvage_without_executor_blocks_stale_cleanup'`
  - Passed: 3 passed, 261 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
