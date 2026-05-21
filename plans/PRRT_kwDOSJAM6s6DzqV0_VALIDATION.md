# PRRT_kwDOSJAM6s6DzqV0 Ownership-repair failure hard-fail validation

Plan reference: `PRRT_kwDOSJAM6s6DzqV0_PLAN.md`

## Requirement Status

- Treat ownership-repair failure as an explicit failure outcome in `_commit_dirty_worktree`: Complete.
- Propagate hard-failure semantics to `_run_fix_cycle`, `_run_sync_base`, and
  `_repair_protected_scope_commits_before_push`: Complete.
- Carry `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE` in hard-failure push results: Complete.
- Add regression tests for the above call paths: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `plans/PRRT_kwDOSJAM6s6DzqV0_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DzqV0_VALIDATION.md`

Commands requested:

- Focused test commands for touched behavior were not executed in this session.
