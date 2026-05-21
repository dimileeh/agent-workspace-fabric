# Review 4496235802 Recovery Guards Plan

## Problem Statement And Scope

The latest review-level comment on PR #272 identifies two remaining
restart-recovery issues in `ControlWorker`:

- `_recover_preserved_active_execution` is called for every active execution
  candidate before runtime inspection, adding several DB round trips in the
  common case where no preserved-active recovery evidence exists.
- The post-salvage PR monitor resume cooldown is process-local, so a worker
  restart loses it and can immediately re-claim a monitor resume that just
  succeeded.

Scope is limited to `src/awf/control/worker.py`, focused worker regression
tests, and this plan/validation pair. No schema changes, branch management
changes, push behavior, or GitHub comments are in scope.

## Requirements Checklist

- Add a lightweight persisted-evidence guard before the first full
  preserved-active recovery dispatch path.
- Preserve direct preserved-active recovery behavior when evidence exists,
  including active worker-restart validation recovery operations.
- Record a DB-side cooldown marker after a salvage-triggered PR monitor resume
  succeeds.
- Make monitor resume claiming honor the persisted cooldown after worker
  restart until the cooldown expires.
- Preserve the existing bounded in-process cooldown behavior.
- Add focused regression tests for the common-path guard and persistent
  cooldown.
- Run targeted unit tests and lint for touched Python files.

## Implementation Steps

1. Add a regression proving an active workspace with no preserved-active
   evidence does not call the full preserved-active recovery path before
   runtime inspection.
2. Add a regression proving a fresh worker skips re-claiming a salvage-attached
   monitor resume while the persisted cooldown event is still current.
3. Confirm the new regressions fail against the current implementation when
   practical.
4. Add a single-query preserved-active recovery evidence helper and gate only
   the initial recovery call in `_recover_stale_active_execution`.
5. Add event constants/helpers for an active-salvage monitor resume cooldown
   marker, record it after successful salvage monitor resume, and consult it in
   monitor claim selection.
6. Re-run targeted tests and ruff.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_recovery_guard_skips_full_recovery_without_evidence or persisted_salvage_monitor_resume_cooldown_survives_worker_restart'`
  must fail before the production change and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_pr_handoff_attaches_one_monitor_after_restart or active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted or preserved_active_recovery_guard_skips_full_recovery_without_evidence or persisted_salvage_monitor_resume_cooldown_survives_worker_restart'`
  must pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  must pass.
