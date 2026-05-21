# Review 4496235802 Recovery Guards Validation

Plan reference: `REVIEW_4496235802_RECOVERY_GUARDS_PLAN.md`

## Requirement Status

- Lightweight persisted-evidence guard before full preserved-active recovery:
  Complete. `_recover_stale_active_execution` now calls the full salvage path
  only when a single-query evidence check finds preserved-active events,
  salvage events, or active worker-restart validation recovery operations.
- Preserve direct recovery when evidence exists: Complete. The guard recognizes
  persisted recovery operations as evidence, and existing direct recovery tests
  remain green.
- Record DB-side cooldown after salvage-triggered PR monitor resume succeeds:
  Complete. Successful salvage monitor resumes now write
  `workspace.active_execution_salvage_monitor_resume_cooldown` with the
  recovery operation ID and cooldown expiry.
- Honor persisted cooldown after worker restart: Complete. Monitor claim
  selection now checks both the bounded in-process cooldown cache and the latest
  persisted cooldown event before claiming a `monitoring_pr` workspace.
- Preserve bounded in-process cooldown behavior: Complete. Existing bounded
  cooldown coverage still passes.
- Focused regression coverage: Complete. Added tests for the no-evidence guard
  and persisted cooldown restart behavior.
- Run targeted tests and lint: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_4496235802_RECOVERY_GUARDS_PLAN.md`
- `plans/REVIEW_4496235802_RECOVERY_GUARDS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_recovery_guard_skips_full_recovery_without_evidence or persisted_salvage_monitor_resume_cooldown_survives_worker_restart'`
  - Failed before the production fix: the no-evidence path called full
    recovery, and the fresh worker re-dispatched monitor resume.
  - Passed after the production fix: 2 passed, 272 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_pr_handoff_attaches_one_monitor_after_restart or active_salvage_monitor_resume_cooldowns_are_bounded_and_expired_entries_are_evicted or preserved_active_recovery_guard_skips_full_recovery_without_evidence or persisted_salvage_monitor_resume_cooldown_survives_worker_restart'`
  - Passed: 4 passed, 270 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/control/test_worker_coverage_edges.py -q -k 'claim_monitoring_pr_ids or active_salvage_monitor_resume_cooldown'`
  - Passed: 3 passed, 331 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Passed.

## Gaps

None.
