# PRRT_kwDOSJAM6s6KZP8f post-commit unowned-delta re-validation plan

## Problem statement
Review thread `PRRT_kwDOSJAM6s6KZP8f` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:687`) reports that
the pre-push dirty finalize's ownership gate is checked **before** calling
`_commit_dirty_worktree`, but that commit sink:

1. runs a **fresh** `git status --porcelain --untracked-files=all`
   (`remote_repair._commit_dirty_worktree` line 340),
2. may invoke protected-scope repair (`_repair_protected_scope_changes_before_commit`,
   which runs the agent CLI via `self._deps.adapter.run(...)`,
   `remote_repair.py` line 963), and
3. then stages **all** non-ignored dirty paths (`git add -A -- <stage_paths>`,
   `remote_repair.py` line 359-361).

If the protected-scope repair (or any process between the gate check and the
fresh staging scan) creates an extra path **outside** `owned_delta_paths`, that
path bypasses the ownership gate (which was computed earlier in
`_try_finalize_pre_push_dirty_repair_state`) and gets committed by the
finalizer. The current code then runs `_pre_push_validation_worktree_check`,
observes a clean tree (the unowned path was committed), and lets the push
proceed — silently sweeping unowned dirt into the PR. This is the same class
of safety gap addressed by thread `PRRT_kwDOSJAM6s6KXLaI` (pre-commit gate)
but on the **post-commit** side: the gate is stale by the time staging runs.

The reviewer asks to "re-validate or pass the allowed path set into the sink
after its side effects before staging."

## Scope
- In `_try_finalize_pre_push_dirty_repair_state`, after a successful commit,
  recompute the operation's committed delta
  (`git diff --name-only operation_start_head..HEAD`) and verify every path is
  still a subset of `owned_delta_paths` (the set computed before the commit).
  If extra paths appear, treat the finalize as fail-closed: return a non-clean
  `ValidationWorktreeCheck` carrying a new dedicated reason code
  (`PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`) so the push is blocked and the
  unowned commit is never silently pushed.
- Add the new reason code constant to `pre_push_validation_constants.py`
  (existing home for pre-push validation reason codes) and thread the import.
- Add a focused regression test that simulates the commit sink committing an
  extra unowned path and asserts the finalize returns the new reason code and
  the push is fail-closed (validation not run).
- No change to `_commit_dirty_worktree`'s shared signature (used by
  `remote_ops`, `ci_ops`, `fix_cycle`, `operator_hints`, `comments`). Passing
  the allowed-path set into the shared sink would widen the change across all
  callers; the post-commit re-validation is the minimal, scoped fix.
- No protected-file edits, no unrelated refactor.

## Requirements checklist
- [ ] Add regression test: a successful finalize commit that introduces a path
      outside `owned_delta_paths` must NOT let validation/push proceed; the
      result must carry `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA` and validation
      must not run. Confirm TDD red against current code.
- [ ] Add `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA` reason-code constant +
      import, and re-export at module level for tests/asserts.
- [ ] Implement the post-commit re-validation in
      `_try_finalize_pre_push_dirty_repair_state` (recompute
      `operation_start_head..HEAD`, compare to `owned_delta_paths`, return a
      non-clean check on mismatch).
- [ ] Existing finalize tests still pass (subset, no-op recheck, policy/
      ownership/protected-scope/provider reason-code preservation, unrelated
      pre-commit dirt).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the failing regression test in
   `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
   (commit sink mocked to return True; queue a `git diff --name-only
   operation_start_head..HEAD` post-commit result that includes an extra
   unowned path; assert `result.passed is False`,
   `result.reason_code == PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`,
   `validation.calls == []`, and that the post-commit recheck is NOT treated
   as clean).
2. Run it, confirm TDD red.
3. Add `_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON` constant to
   `pre_push_validation_constants.py`, import + re-export in
   `pre_push_validation.py`.
4. Implement the post-commit re-validation branch in
   `_try_finalize_pre_push_dirty_repair_state` after `committed` is True and
   before the existing verify recheck.
5. Re-run the new + existing finalize tests (TDD green).
6. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria
- New post-commit unowned-delta regression test fails on the unfixed code and
  passes on the fixed code.
- Existing finalize tests still pass.
- Lint/typecheck clean on touched files.
- Diff stays minimal and scoped to this thread's concern.
