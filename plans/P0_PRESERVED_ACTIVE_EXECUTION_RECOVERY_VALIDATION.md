# P0 Preserved Active Execution Recovery Validation

Plan reference: `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`

Implementation contract reference: `docs/awf-plans/ws_77bb4cce4aea4892bb41e0e6.md`

## Requirement Status

- Complete: Treat `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART` as recovery start.
  - Evidence: `src/awf/control/worker.py` now invokes preserved-active recovery after recording preservation and on later scans.
- Complete: Recover existing PRs by attaching one PR monitor.
  - Evidence: `test_preserved_active_pr_handoff_attaches_one_monitor_after_restart`.
- Complete: Recover clean committed work through validation continuation.
  - Evidence: `test_preserved_active_clean_committed_work_dispatches_validation_salvage_once`; `src/awf/control/executor.py` accepts `worker_restart` validate-only recovery.
- Complete: Create one lineage-preserving replacement when no usable work exists.
  - Evidence: `test_preserved_active_without_usable_work_creates_one_replacement_with_lineage`.
- Complete: Leave ambiguous cases operator-recoverable without failed runtime release.
  - Evidence: `test_preserved_active_ambiguous_dirty_worktree_is_operator_recoverable`.
- Complete: Preserve stale-active failure for true orphan/no-recovery cases.
  - Evidence: `test_stale_active_failure_still_applies_after_salvage_not_possible_for_orphan`.
- Complete: Preserve reason codes, events, operations, and task/attempt lineage.
  - Evidence: new salvage reason codes/events and operation payload assertions in `tests/unit/control/test_worker.py`.
- Complete: Keep salvage idempotent across repeated scans.
  - Evidence: repeated `worker.run_once()` assertions in the new regression tests.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
# 191 passed in 215.17s

uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/service -q
# 2564 passed in 1122.24s

uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control tests/unit/service
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 157 source files
```

## Files Changed

- `src/awf/control/worker.py`
- `src/awf/control/executor.py`
- `tests/unit/control/test_worker.py`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_PLAN.md`
- `plans/P0_PRESERVED_ACTIVE_EXECUTION_RECOVERY_VALIDATION.md`

No known gaps remain for this plan slice.
