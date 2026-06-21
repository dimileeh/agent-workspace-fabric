# Plan: PRRT_kwDOSJAM6s6Kg4JR — CI-fix provider-recovery residue rollback

## Problem statement and scope

Inline review thread `PRRT_kwDOSJAM6s6Kg4JR` (PR #615,
`src/awf/runtime/pr_monitor_runner/ci_ops.py:262`) reports that
`_run_ci_fix` does not roll back repair residue to
`operation_start_head` when `_commit_dirty_worktree` raises a
provider-recovery control-flow exception
(`ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
`ProviderRecoveryAuthError`) from protected-scope repair inside the
commit sink.

The existing commit-sink exception handlers
(`ProtectedScopeDiffError`,
`_MonitorAgentRuntimeOwnershipRepairFailedError`, `_MonitorPolicyBlockedError`)
already record the provider state and return a terminal reason-coded
result, and the "stranded dirty" branch records the provider state and
returns `REPAIR_DIRTY_COMMIT_FAILED`. But the *clean* commit-sink path
(`committed is True`) still calls
`await self._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)`
at `ci_ops.py:262`, which raises one of the three provider-recovery
control-flow exceptions. Those propagate WITHOUT rolling the worktree
back to `operation_start_head`. If the protected-scope repair agent
left repair dirt in the worktree (e.g. a partial protected-file revert
plus a successful commit of the unrelated CI fix, or dirt left by the
agent run itself before the commit succeeded), that residue strands
and the next monitor attempt trips
`_pre_existing_dirty_repair_worktree_result` as
`PRE_EXISTING_DIRTY_WORKTREE`, hiding the provider outage and wedging
the workspace instead of letting the provider retry actually run.

This mirrors the fix-pass residue rollback
(`PRRT_kwDOSJAM6s6Kc_Ak`) and the finalize residue rollback
(`PRRT_kwDOSJAM6s6KewGH`): every other `_commit_dirty_worktree` caller
rolls back the operation's repair residue to the operation-start HEAD
before re-raising a provider-recovery control-flow exception, so the
next repair cycle's repair-start guard does not wedge on stranded
dirt.

## Scope

- `src/awf/runtime/pr_monitor_runner/ci_ops.py`: in `_run_ci_fix`, wrap
  the `await self._handle_provider_agent_run_error(...)` call at line
  262 in a `try/except` for the three provider-recovery control-flow
  exceptions; on any of them, roll the worktree back to
  `operation_start_head` (mirroring the finalize rollback) and re-raise
  so the monitor loop's dedicated handlers still surface
  `PROVIDER_OUTAGE` / `PROVIDER_FALLBACK` / auth-failed semantics. A
  rollback failure is logged but never clobbers the pending
  provider-recovery exception.

- Tests: add regression tests for all three exceptions proving the
  residue is rolled back to `operation_start_head` and the exception
  still propagates.

Out of scope:
- Pre-push fix-pass / finalize paths (already rolled back).
- Refactoring unrelated callers.
- Broad validation suites (AWF owns those post-agent).

## Explicit requirements checklist

1. `_run_ci_fix` rolls the worktree back to `operation_start_head`
   before re-raising any of `ProviderRecoveryRetryError`,
   `ProviderRecoveryFallbackError`, `ProviderRecoveryAuthError` raised
   by `_handle_provider_agent_run_error` on the clean commit path.
2. The provider-recovery exception still propagates so the monitor
   loop's dedicated handlers surface `PROVIDER_OUTAGE` /
   `PROVIDER_FALLBACK` / auth-failed semantics.
3. A rollback failure is logged but never clobbers the pending
   provider-recovery exception.
4. Behavior is unchanged for the existing commit-sink exception
   handlers and the `REPAIR_DIRTY_COMMIT_FAILED` stranded-dirty branch
   (regression tests already cover those).
5. Regression tests exist for all three provider-recovery exceptions
   asserting (a) the exception propagates, (b) the worktree is rolled
   back to `operation_start_head`.
6. No protected workflow / quality-gate / configuration files are
   touched.
7. Coverage is preserved / improved on the touched branches.

## Implementation steps

1. In `ci_ops.py`, add a focused helper
   `_rollback_ci_fix_residue_before_provider_recovery` modeled on
   `_rollback_finalize_dirty_residue_before_provider_recovery` in
   `pre_push_validation.py`. It:
   - returns early if `operation_start_head` is falsy (no anchor;
     cannot safely restore; residue strands visibly), logging a
     warning.
   - otherwise runs `git reset --hard <operation_start_head>` and
     `git clean -ffd -- <dirt>` is NOT needed here because the
     operation's owned residue is tracked edits the agent left after
     the successful commit; the simpler `git reset --hard
     operation_start_head` mirrors the fix-pass rollback
     (`_rollback_failed_pre_push_validation_fix_pass`) and discards
     both staged and working-tree changes back to the operation-start
     HEAD. (The fix-pass and finalize rollbacks both use this exact
     shape.)
   - logs a warning on rollback failure but never raises; the pending
     provider-recovery exception takes priority.

2. In `_run_ci_fix`, replace the bare
   `await self._handle_provider_agent_run_error(...)` at line 262 with
   a `try/except` that calls the rollback helper for each of the three
   provider-recovery exceptions and re-raises.

3. Add regression tests in
   `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`
   (or a sibling part if line limits require it) parametrized over the
   three exception classes, mirroring
   `test_pre_push_validation_finalize_rolls_back_dirty_residue_before_provider_recovery`.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py -q`

Full AWF / GitHub CI broad validation is owned by AWF after agent
completion and is NOT run in this agent phase.

Pass criteria:
- New regression tests pass.
- Existing `test_ci_fix_dirty_commit_failed_*` and
  `test_ci_fix_provider_retry_commits_dirty_output_before_retry`
  tests still pass unchanged.
- No ruff / mypy regressions on touched files.
