# Problem Statement

Inline review thread `PRRT_kwDOSJAM6s6DcvTq` reports that worker-restart salvage can rewind a `validating` or `pushing` workspace back to `running` and create a new `worker_restart` validation operation while leaving the original active operation pending or running.

# Scope

Address preserved active-execution validation salvage in `src/awf/control/worker.py` for non-running active phases. Keep behavior scoped to the abandoned active operation records for `validating` and `pushing` workspaces.

# Requirements Checklist

- Add a regression test proving a pre-existing active non-`worker_restart` validate/push operation is terminal after preserved active-execution validation salvage.
- Preserve the new `worker_restart` `validate_only` recovery operation and existing salvage event semantics.
- Do not weaken existing recovery, stale detection, or state-machine behavior.
- Validate with the narrow targeted test first, then run an appropriate worker test slice.

# Implementation Steps

1. Add a focused regression test in `tests/unit/control/test_worker.py`.
2. Confirm the new test fails against current behavior.
3. Implement operation finalization in the preserved active validation request path.
4. Re-run the targeted test and a broader worker recovery slice.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6DcvTq_VALIDATION.md`.

# Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage or preserved_active_rewound_validation_salvage_waits_without_duplicate_when_slots_full or preserved_active_pushed_branch_pr_lookup_failure_falls_back_to_worktree_salvage" -q
```

Pass criteria: the targeted regression fails before implementation, then passes with the worker recovery slice after implementation.
