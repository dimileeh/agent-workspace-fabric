# Review 4496235802 Resume Cooldown Plan

## Problem Statement And Scope

The PR review reports that `ControlWorker` bounds active salvage monitor
recovery operation IDs, but leaves
`_active_salvage_monitor_resume_cooldowns` unbounded. Cooldown entries are only
removed when the same workspace is checked after expiration, so terminal or
otherwise unscheduled workspaces can leave stale entries in a long-running
worker.

Scope is limited to `ControlWorker`'s in-memory active-salvage monitor resume
cooldown cache, focused regression coverage, and validation notes. The existing
`VALIDATION_REQUESTED` plus executor-unavailable salvage path already records
`SALVAGE_BLOCKED` and has regression coverage, so this plan only verifies that
as pre-existing behavior.

## Requirements Checklist

- Preserve the current protection for `VALIDATION_REQUESTED` salvage when the
  current worker has no executor.
- Add a bounded cache for active salvage monitor resume cooldowns.
- Evict expired cooldown entries even when the expired workspace is not the one
  currently being checked or inserted.
- Evict oldest live cooldown entries when the cache exceeds its limit.
- Preserve existing cooldown semantics for live entries.
- Add a regression proving expired entries are pruned and oldest live entries
  are evicted over the limit.
- Run the narrow affected unit tests and lint for touched Python files.

## Implementation Steps

1. Add a focused unit test beside
   `test_active_salvage_monitor_recovery_operation_ids_are_bounded` that seeds
   an expired cooldown and more live cooldowns than the configured limit.
2. Confirm the new test fails against the current implementation.
3. Add a resume cooldown limit constant and helper methods to remember
   cooldowns and evict expired/oldest entries.
4. Route monitor resume cooldown writes through the bounded helper and prune
   expired entries during cooldown checks.
5. Re-run the targeted tests and ruff for touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted'`
  must fail before the production change and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'active_salvage_monitor_recovery_operation_ids_are_bounded or active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted or preserved_active_validation_salvage_without_executor_blocks_stale_cleanup'`
  must pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  must pass.
