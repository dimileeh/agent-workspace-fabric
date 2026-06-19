# Plan: PRRT_kwDOSJAM6s6KOFUR — Deposit planning artifacts on validation stop paths

## Problem statement and scope

Review comment on `src/awf/control/executor/execution_validation.py:742` (PR #608)
notes that the post-validation success deposit (line 749) only runs on the normal
validation-success branch. When a planned workspace reaches a callback-terminal
state during validation, `_finish_validation_callback_if_terminal()` returns
`True` and `run_validation_and_fix_cycle()` returns `stop=True` *before* that
block runs. Because the old unconditional deposit in `execution_flow.execute` has
been removed, the preserved worktree's plan/conformance files are never copied to
`work/artifacts/{workspace_id}` and the console can observe the terminal row update
without ever refetching those artifacts.

Scope: keep a **best-effort** deposit on the callback-terminal stop paths inside
`run_validation_and_fix_cycle()` without re-introducing the broad
post-validation deposit block or touching unrelated code.

## Requirements checklist

1. When `_finish_validation_callback_if_terminal()` returns `True` inside
   `run_validation_and_fix_cycle()`, deposit planning artifacts before the
   function returns `stop=True`.
2. Preserve the existing ordering invariant: deposit runs **before** any terminal
   status update that bumps `workspace.updated_at` (console artifact refresh
   key).
3. Do not run a redundant deposit on the normal success path (the inline
   satisfied-conformance deposit and `_run_post_validation_conformance_check`
   already handle it).
4. Gate the deposit on `profile.planning.required` via the existing
   `_deposit_planning_artifacts_best_effort()` helper.
5. Add or update regression tests proving the deposit order for callback-terminal
   paths.
6. Keep changes minimal and scoped to the review comment.

## Implementation steps

1. Introduce a small internal helper inside `run_validation_and_fix_cycle()`
   that calls `_planning_artifacts._deposit_planning_artifacts_best_effort()`
   before returning `stop=True`. Reuse the existing `profile` / `worktree_path`
   closure state.
2. Replace the three bare `return ExecutionValidationResult(stop=True, ...)` at
   lines 192 (stale start transition), 281 (stale recheck inside loop), and 512
   (stale callback after normal cleanup) with a deposit-then-return sequence.
3. The other `stop=True` returns either already use `_mark_failed_preserving_planning_artifacts`
   / `_enter_blocked_preserving_planning_artifacts` / `_fail_validation_worktree_guard`
   (which deposit internally), or occur in contexts where planning deposit is not
   relevant (e.g. git/add/diff failures in the middle of a fix pass, not a
   preserved-FAILED terminal workspace). Verify each existing stop path and
   document the rationale.
4. Add regression tests in the existing focused edge-coverage suite:
   - Stale start transition deposits artifacts before returning.
   - Stale recheck deposits artifacts before returning.
   - Stale callback after cleanup deposits artifacts before returning.
5. Run focused unit tests for touched files and the narrow lint/type checks.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py tests/unit/control/test_validation_worktree_guard_deposit_ordering.py tests/unit/control/test_planning_artifacts_deposit_ordering.py -q` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py` passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py` passes.

## Assumptions/Changes

- The existing helpers `_mark_failed_preserving_planning_artifacts` and
  `_enter_blocked_preserving_planning_artifacts` already deposit before marking
  FAILED / BLOCKED.
- `_fail_validation_worktree_guard` (used for pre-validation guard failures and
  post-fix-pass dirty worktree) also deposits before marking FAILED.
- Callback-terminal returns at the start-transition (line 192), recheck
  (line 281), and post-cleanup callback (line 512) are the paths the reviewer
  explicitly identifies; these are the ones we will instrument.
- The broader suite (full pytest, full build) is owned by AWF/GitHub CI per the
  workspace contract; we will run only the narrow, focused checks above.
