# Validation: PRRT_kwDOSJAM6s6Kg4JR — CI-fix provider-recovery residue rollback

Plan reference: `plans/PRRT_kwDOSJAM6s6Kg4JR_CI_FIX_PROVIDER_RECOVERY_RESIDUE_PLAN.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | `_run_ci_fix` rolls the worktree back to `operation_start_head` before re-raising any of `ProviderRecoveryRetryError`, `ProviderRecoveryFallbackError`, `ProviderRecoveryAuthError` raised by `_handle_provider_agent_run_error` on the clean commit path. | Complete | `src/awf/runtime/pr_monitor_runner/ci_ops.py`: second `try/except` wrapping the `await self._handle_provider_agent_run_error(...)` call (was line 262, now ~line 346) calls `_rollback_ci_fix_residue_before_provider_recovery(...)` then `raise`. |
| 2 | The provider-recovery exception still propagates so the monitor loop's dedicated handlers surface `PROVIDER_OUTAGE` / `PROVIDER_FALLBACK` / auth-failed semantics. | Complete | `raise` after the rollback call in both new `except` clauses. Verified by `test_ci_fix_provider_recovery_rolls_back_residue_before_re_raise` (parametrized over all three exceptions) which asserts `pytest.raises(type(raised_exc))`. |
| 3 | A rollback failure is logged but never clobbers the pending provider-recovery exception. | Complete | `_rollback_ci_fix_residue_before_provider_recovery` logs `monitor.ci_fix_provider_recovery_rollback_failed` on `git reset --hard` failure and returns without raising. Verified by `test_ci_fix_provider_recovery_rollback_failure_does_not_clobber_exception`. |
| 4 | Behavior is unchanged for the existing commit-sink exception handlers and the `REPAIR_DIRTY_COMMIT_FAILED` stranded-dirty branch. | Complete | No edits to the `ProtectedScopeDiffError` / `_MonitorAgentRuntimeOwnershipRepairFailedError` / `_MonitorPolicyBlockedError` handlers or the stranded-dirty branch. Existing regression tests `test_ci_fix_dirty_commit_failed_surfaces_terminal_result_not_provider_retry` and `test_ci_fix_dirty_commit_failed_status_recheck_failure_preserved` pass unchanged. |
| 5 | Regression tests exist for all three provider-recovery exceptions asserting (a) the exception propagates, (b) the worktree is rolled back to `operation_start_head`. | Complete | `test_ci_fix_provider_recovery_rolls_back_residue_before_re_raise` (clean commit path, parametrized over 3 exceptions) and `test_ci_fix_commit_sink_provider_recovery_rolls_back_residue_before_re_raise` (commit-sink-raised path, parametrized over 3 exceptions). |
| 6 | No protected workflow / quality-gate / configuration files are touched. | Complete | Only `src/awf/runtime/pr_monitor_runner/ci_ops.py` and `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py` changed. |
| 7 | Coverage is preserved / improved on the touched branches. | Complete | Both new `except` clauses (commit-sink-raised and clean-commit-path) are exercised by parametrized regression tests. The rollback helper's success and failure branches are both covered (`_rollback_ci_fix_residue_before_provider_recovery`'s `reset.ok` True path via the parametrized tests; False path via the rollback-failure test). |

## Files changed

- `src/awf/runtime/pr_monitor_runner/ci_ops.py`:
  - Added `_rollback_ci_fix_residue_before_provider_recovery` helper (mirrors `_rollback_finalize_dirty_residue_before_provider_recovery` in `pre_push_validation.py` and `_rollback_failed_pre_push_validation_fix_pass` in `pre_push_validation_fix_pass.py`).
  - Added `except (ProviderRecoveryRetryError, ProviderRecoveryFallbackError, ProviderRecoveryAuthError)` around `_commit_dirty_worktree` (commit-sink-raised path) — rollback + re-raise.
  - Wrapped the `await self._handle_provider_agent_run_error(...)` call on the clean commit path in a `try/except` for the same three exceptions — rollback + re-raise.
  - Added `git_worktree_command` import.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`:
  - `test_ci_fix_provider_recovery_rolls_back_residue_before_re_raise` (parametrized over the 3 exceptions; clean commit path).
  - `test_ci_fix_provider_recovery_rollback_failure_does_not_clobber_exception` (rollback failure does not swallow the recovery exception).
  - `test_ci_fix_commit_sink_provider_recovery_rolls_back_residue_before_re_raise` (parametrized over the 3 exceptions; commit-sink-raised path).
- `plans/PRRT_kwDOSJAM6s6Kg4JR_CI_FIX_PROVIDER_RECOVERY_RESIDUE_PLAN.md` (this plan).
- `plans/PRRT_kwDOSJAM6s6Kg4JR_CI_FIX_PROVIDER_RECOVERY_RESIDUE_VALIDATION.md` (this validation).

## Verification commands run (focused, in-agent)

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py
# Success: no issues found in 1 source file

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py -q
# 12 passed in 14.40s  (5 existing + 7 new)

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
# 309 passed in 294.69s  (no regressions in the broader PR-monitor-runner suite)

uv run --python 3.12 --extra dev pytest tests/unit/runtime/ -k "ci_fix or ci_repair or provider_recovery" -q
# 56 passed, 2331 deselected in 65.02s
```

Full AWF / GitHub CI broad validation (full coverage gate, full frontend build, whole-repository test suites) is owned by AWF after agent completion and was NOT run in this agent phase, per the workspace contract.

## Iteration gaps

None. All planned requirements are Complete.
