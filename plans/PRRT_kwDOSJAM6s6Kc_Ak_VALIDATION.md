# PRRT_kwDOSJAM6s6Kc_Ak validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Kc_Ak_PLAN.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Add a regression test (TDD red) parametrized over the three provider-recovery exceptions, with `_commit_dirty_worktree` raising AND leaving the worktree dirty, asserting the fix-pass calls `reset --hard fix_start_head` (via `_rollback_failed_fix_pass`) BEFORE re-raising. | Complete | `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_provider_retry` |
| 2 | Confirm the new test fails on the current code (TDD red). | Complete | Before the fix, all three parametrized cases failed with `assert not True` (`reset --hard` was not queued). The exception propagated (so `pytest.raises` passed), but no rollback ran. |
| 3 | Implement the minimal fix in `pre_push_validation_fix_pass.py` (split the combined except clause; roll back then re-raise for provider-recovery exceptions only). | Complete | `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py:443` — the combined `except (...)` clause is split into two clauses: one for the three provider-recovery exceptions (rollback via `_rollback_failed_fix_pass` then re-raise), one for the three deterministic reason-coded exceptions (unchanged re-raise). |
| 4 | Confirm the new test passes (TDD green). | Complete | All three parametrized cases pass after the fix. |
| 5 | Confirm the existing `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions` still passes. | Complete | The test's parametrization was narrowed from all 6 exceptions to the 3 deterministic ones (`ProtectedScopeDiffError`, `_MonitorPolicyBlockedError`, `_MonitorAgentRuntimeOwnershipRepairFailedError`); the 3 provider-recovery exceptions now have their own dedicated regression test. The deterministic-path assertion (no `reset --hard` when the worktree is clean) still holds. Docstring updated to explain the split. |
| 6 | Targeted lint + typecheck on touched files only. | Complete | `ruff check` and `mypy` both clean on the touched source file; `ruff check` clean on the touched test file. |

## Evidence

### Files changed

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
  - Split the combined `except (...)` clause at line 443 into two clauses:
    1. `except (ProviderRecoveryAuthError, ProviderRecoveryFallbackError, ProviderRecoveryRetryError)`: attempt `_rollback_failed_pre_push_validation_fix_pass(...)` (logging any rollback-failure reason but never clobbering the recovery exception), then `raise`.
    2. `except (ProtectedScopeDiffError, _MonitorAgentRuntimeOwnershipRepairFailedError, _MonitorPolicyBlockedError)`: `raise` (unchanged).

- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
  - Narrowed the existing `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions` parametrization to the 3 deterministic reason-coded exceptions (the provider-recovery ones now have their own dedicated test). Updated docstring + comment to explain the split.
  - Added `test_pre_push_validation_fix_pass_rolls_back_dirty_residue_before_provider_retry` parametrized over the three provider-recovery exceptions, asserting the fix-pass rolls back to `fix_start_head` BEFORE re-raising.

### Tests/commands run (focused — broad validation owned by AWF/GitHub)

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q` → 22 passed.
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py -q` → 118 passed.
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts -q` → 271 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py` → All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` → Success: no issues found.

## Pass criteria

- New regression test fails on unfixed code, passes on fixed code. ✅
- Existing `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions` still passes (deterministic handler path unchanged; provider-recovery cases split into dedicated regression). ✅
- Lint/typecheck clean on touched files. ✅

## Notes

- The fix mirrors the strand-risk pattern already addressed in `ci_ops.py`
  (the `REPAIR_DIRTY_COMMIT_FAILED` terminal result, review thread
  `PRRT_kwDOSJAM6s6KY4Wi`) and at the finalize path
  (`pre_push_validation.py:872-912`). The pre-push fix-pass was the last
  `_commit_dirty_worktree` caller that re-raised provider-recovery
  exceptions without rolling back the residue the agent left behind.
- The rollback-failure reason is intentionally swallowed (logged only): a
  failed cleanup must not clobber the recovery control-flow exception,
  because the loop's dedicated handlers (`PROVIDER_OUTAGE` /
  `PROVIDER_FALLBACK` / auth-failed) must still run. If the rollback
  fails, the stranded residue surfaces on the next attempt as
  `PRE_EXISTING_DIRTY_WORKTREE` (same fail-closed behavior as the
  existing `commit_exception` rollback path).
- Full AWF/GitHub broad validation (full coverage gate, full frontend
  build, whole-repository test suite) is managed by AWF after agent
  completion and was NOT run in the agent phase.
