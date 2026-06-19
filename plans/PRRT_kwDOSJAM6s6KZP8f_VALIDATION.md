# PRRT_kwDOSJAM6s6KZP8f validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6KZP8f_PLAN.md`

## Verdict
FIXED

## Summary
The pre-push dirty finalize now re-validates the operation's committed delta
*after* `_commit_dirty_worktree`'s side effects and fails closed with a
dedicated `PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA` reason code if the commit
introduced any path outside the pre-commit `owned_delta_paths`, so an unowned
path created by protected-scope repair (or any process between the gate
check and the sink's fresh staging scan) is never silently pushed.

## Evidence (focused checks only — broad AWF/GitHub validation owned by AWF)
- New regression test
  `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`
  failed on the unfixed code (TDD red: `ImportError` for the missing reason
  constant) and passes on the fixed code.
- Full finalize suite:
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q`
  -> 17 passed.
- Related pre-push validation suites (validation, repairs, repairs_validated_push,
  mixed_127, fix_pass parts):
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ -q`
  -> all passed.
- Broader pr_monitor/pre-push slice:
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/ -q -k "pre_push_validation or pr_monitor_pre_push or validated_push or pr_monitor_runner"`
  -> 547 passed, 1723 deselected.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
  -> All checks passed.
- Typecheck:
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py`
  -> Success: no issues found.
- Maintainability line-limit:
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/test_core_decomposition_maintainability.py -q`
  -> 9 passed.

## Scope discipline
- Touched files:
  - `src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py`
    (added one reason-code constant + comment).
  - `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` (import +
    re-export of the new constant; post-commit re-validation branch in
    `_try_finalize_pre_push_dirty_repair_state`; docstring update).
  - `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
    (new regression test; added two queued post-commit diff results to the two
    existing finalize tests whose commit sink returns True, so the new
    re-validation has the git diff results it needs).
- No change to the shared `_commit_dirty_worktree` signature or its other
  callers (`remote_ops`, `ci_ops`, `fix_cycle`, `operator_hints`, `comments`).
- No protected-file edits, no unrelated refactor, no new abstractions.

## Gap analysis
None. The fix is minimal and scoped to the reported concern (the stale
ownership gate after the commit sink's side effects). The post-commit
re-validation reuses the existing `_operation_owned_delta_paths` helper, so no
new git-diff logic is introduced.
