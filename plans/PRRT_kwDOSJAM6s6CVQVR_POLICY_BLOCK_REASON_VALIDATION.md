# PRRT_kwDOSJAM6s6CVQVR Policy Block Reason Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CVQVR_POLICY_BLOCK_REASON_PLAN.md`

## Requirement Status

- Add a regression test proving a CI-repair policy block returns
  `MONITOR_POLICY_BLOCKED`: Complete.
- Keep existing protected-scope and generic git-push semantics unchanged:
  Complete for this diff. The implementation changes only the CI-repair
  `_MonitorPolicyBlockedError` result branch.
- Make the smallest code change needed to preserve the policy-block reason:
  Complete.
- Run focused tests covering the new regression and nearby monitor behavior:
  Complete for targeted policy-block behavior; partial for the broader
  coverage-edge file because unrelated protected-scope tests fail.
- Commit the fix locally without switching branches or pushing: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `plans/PRRT_kwDOSJAM6s6CVQVR_POLICY_BLOCK_REASON_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CVQVR_POLICY_BLOCK_REASON_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocking_supply_chain_finding_is_not_committed_or_pushed -q`
  - Before implementation: failed with `GIT_PUSH_FAILED` instead of
    `MONITOR_POLICY_BLOCKED`.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocking_supply_chain_finding_is_not_committed_or_pushed tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_policy_block_uses_specific_reason -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Failed: 4 protected-scope repair tests:
    `test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry`,
    `test_protected_scope_commit_repair_fails_when_commit_returns_false_with_dirty_worktree`,
    `test_ci_fix_commits_verified_protected_revert_during_scope_repair`, and
    `test_ci_fix_stops_when_protected_revert_diff_baseline_unavailable`.
  - These failures occur outside the changed CI policy-block branch and were
    not addressed in this review-thread-specific fix.

## Iteration 1

The highest-impact planned gap was the missing CI-repair policy-block reason.
The new failing assertion reproduced it, the handler now returns
`MONITOR_POLICY_BLOCKED`, and the targeted regression passes.

Remaining gap: the broader coverage-edge file has unrelated protected-scope
repair failures. That should be handled as a separate protected-scope repair
test/behavior task rather than folded into thread
`PRRT_kwDOSJAM6s6CVQVR`.
