# PRRT_kwDOSJAM6s6Kc_Ak roll back validation-fix dirt before provider retry

## Problem statement

The inline review thread `PRRT_kwDOSJAM6s6Kc_Ak` (discussion
r3433769929) points at
`src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py:447`
(`_run_pre_push_validation_fix_pass`).

When the validation-fix agent leaves protected-scope edits in the
worktree, `_commit_dirty_worktree` enters protected-scope repair
(`_repair_protected_scope_changes_before_commit`). That repair can raise
a provider-recovery control-flow exception:

- `ProviderRecoveryRetryError` — raised directly at
  `remote_repair_protected.py:809` when
  `_provider_recovery_suppresses_cli` is true, OR
- `ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
  `ProviderRecoveryAuthError` — raised by
  `_handle_provider_agent_run_error` at `remote_repair_protected.py:821`
  (see `provider_ops.py:288-304`).

The fix-pass catch at
`pre_push_validation_fix_pass.py:443-460` re-raises these three
exceptions BEFORE `_rollback_failed_fix_pass` runs. The monitor loop
(`loop.py:972-1002` etc.) records `PROVIDER_OUTAGE` /
`PROVIDER_FALLBACK` / auth-failed and re-raises without cleaning the
worktree. The protected-scope edits the agent left remain dirty. On the
next monitor attempt, `_pre_existing_dirty_repair_worktree_result`
(`remote_repair.py:61`) / the pre-push validation worktree check trips
as `PRE_EXISTING_DIRTY_WORKTREE`, masking the provider outage and
wedging the PR.

The existing regression test
`test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions`
(`test_pr_monitor_pre_push_validation_fix_pass_part_002.py:228`)
parametrizes over `ProviderRecoveryRetryError` /
`ProviderRecoveryFallbackError` / `ProviderRecoveryAuthError` and
asserts the rollback handler must NOT run. That test's
`_commit_dirty_worktree` monkeypatch raises immediately WITHOUT leaving
residue — so it only covers the "worktree was already clean" path, not
the "agent left protected-scope dirt" path the reviewer describes.

This is the same strand-risk class already addressed in `ci_ops.py`
(`REPAIR_DIRTY_COMMIT_FAILED` terminal result,
`test_pr_monitor_runner_coverage_edges_part_011.py:230`) and at the
finalize path (`pre_push_validation.py:872-912`).

## Fix

In `_run_pre_push_validation_fix_pass`, when `_commit_dirty_worktree`
raises one of the three provider-recovery exceptions, FIRST attempt to
roll back the fix-pass residue to `fix_start_head` (via
`_rollback_failed_pre_push_validation_fix_pass`), suppressing any
rollback-failure reason (a failed cleanup is logged but must not clobber
the recovery control-flow exception), THEN re-raise the recovery
exception so the monitor loop's dedicated handlers still surface the
right reason code.

The other reason-coded exceptions (`ProtectedScopeDiffError`,
`_MonitorPolicyBlockedError`,
`_MonitorAgentRuntimeOwnershipRepairFailedError`) are NOT in scope: they
represent deterministic commit-sink failures (no provider outage), the
worktree-residue risk is the same but the recovery semantics differ, and
the reviewer's comment is specifically about the provider-recovery
branch. Leave their existing re-raise unchanged.

Minimal, scoped change: split the combined `except (...)` clause into
two clauses — one for the three provider-recovery exceptions (roll back
then re-raise), one for the other three reason-coded exceptions
(unchanged re-raise).

## Scope

- Only
  `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
  lines 443-460 (the combined `except (...)` clause). No other file
  changes.
- Add a regression test in
  `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
  that asserts: when `_commit_dirty_worktree` raises
  `ProviderRecoveryRetryError` AND leaves residue, the fix-pass
  rolls back to `fix_start_head` BEFORE re-raising. Parametrize over
  the three provider-recovery exceptions.
- Update the existing
  `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions`
  test to keep covering the "clean-when-raised" path (its current
  behavior): the rollback is now attempted but the queued git outputs
  keep the worktree clean, so no `reset --hard` runs. Verify the
  existing assertions still hold.
- No refactor, no new abstractions, no protected-file edits.

## Requirements checklist

- [ ] Add a regression test (TDD red) parametrized over the three
      provider-recovery exceptions, with `_commit_dirty_worktree`
      raising AND leaving the worktree dirty, asserting the fix-pass
      calls `reset --hard fix_start_head` (via `_rollback_failed_fix_pass`)
      BEFORE re-raising the recovery exception.
- [ ] Confirm the new test fails on the current code (TDD red).
- [ ] Implement the minimal fix in `pre_push_validation_fix_pass.py`
      (split the combined except clause; roll back then re-raise for
      provider-recovery exceptions only).
- [ ] Confirm the new test passes (TDD green).
- [ ] Confirm the existing
      `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions`
      still passes (its `_commit_dirty_worktree` raises without residue,
      so the rollback helper's `git status` reports clean and no
      `reset --hard` is queued; the existing assertion holds).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps

1. Write the regression test parametrized over
   `ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
   `ProviderRecoveryAuthError`. The `_commit_dirty_worktree` monkeypatch
   raises one of these. Queue `git status` to report a dirty path so the
   rollback helper's `_pre_push_validation_cleanup` runs `reset --hard
   fix_start_head` + `clean -ffd <path>` before re-raising. Assert the
   recovery exception is raised AND a `reset --hard fix_start_head`
   command was queued.
2. Run it, confirm it fails (TDD red — currently the rollback is not
   attempted for provider-recovery exceptions).
3. In `pre_push_validation_fix_pass.py`, split the combined `except (...)`
   clause into:
   - `except (ProviderRecoveryAuthError, ProviderRecoveryFallbackError,
     ProviderRecoveryRetryError)`: attempt
     `_rollback_failed_pre_push_validation_fix_pass(...)` (swallowing any
     rollback-failure reason — log it but do not return), then `raise`.
   - `except (ProtectedScopeDiffError,
     _MonitorAgentRuntimeOwnershipRepairFailedError,
     _MonitorPolicyBlockedError)`: `raise` (unchanged).
4. Re-run the new test (TDD green) and the existing regression test.
5. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`

## Pass criteria

- New regression test fails on unfixed code, passes on fixed code.
- Existing
  `test_pre_push_validation_fix_pass_preserves_reason_coded_commit_exceptions`
  still passes (clean-when-raised path unchanged).
- Lint/typecheck clean on touched files.
