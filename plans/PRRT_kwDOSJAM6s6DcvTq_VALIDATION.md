# Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DcvTq_PLAN.md`

# Requirement Status

- Regression test for pre-existing active non-`worker_restart` validate/push operation: Complete.
- Preserve new `worker_restart` `validate_only` recovery operation and salvage semantics: Complete.
- Do not weaken existing recovery, stale detection, or state-machine behavior: Complete.
- Validate with narrow targeted test and worker recovery slice: Complete.

# Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DcvTq_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DcvTq_VALIDATION.md`

Tests and checks:

- Failing-before evidence: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage -q` failed with the seeded original operation still `running`.
- Passing-after evidence: same targeted command passed with `2 passed`.
- Recovery slice: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_rewound_validation_salvage_waits_without_duplicate_when_slots_full or preserved_active_pushed_branch_pr_lookup_failure_falls_back_to_worktree_salvage" -q` passed with `4 passed, 198 deselected`.
- Static checks: `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- Type check: `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py` passed.

# Result

All planned requirements are complete. No follow-up iteration is required.
