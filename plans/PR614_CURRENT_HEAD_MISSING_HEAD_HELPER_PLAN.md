# PR614 Current Head Missing-HEAD Helper Plan

## Problem Statement And Scope

PR #614 CI previously failed in `python-coverage-shards (3)` while running
`tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure`.
The log showed `AttributeError: '_Executor' object has no attribute '_recover_missing_git_head_or_mark_failed'`,
which converted the expected `EXEC_PROCESS_CLEANUP_FAILED` cleanup failure into an unexpected infrastructure
error. Current HEAD contains optional-helper handling for setup and agent cleanup recovery paths, but
`execution_flow.execute` still has direct calls to `_recover_missing_git_head_or_mark_failed` in generic
missing-HEAD exception handlers.

Scope is limited to those direct call sites and a focused regression test. No workflow/config changes, no
branch changes, no push, and no broad AWF/GitHub validation.

## Requirements Checklist

- Preserve missing-HEAD recovery behavior when an executor provides `_recover_missing_git_head_or_mark_failed`.
- Avoid `AttributeError` when a focused/minimal executor fixture lacks that helper.
- Keep cleanup-failure reason codes/messages intact instead of hiding them behind an unexpected-error wrapper.
- Add a behavior-focused regression test for the missing-helper path.
- Run only targeted local tests for touched behavior; leave broad validation to AWF/GitHub after this agent exits.

## Implementation Steps

1. Inspect the direct `_recover_missing_git_head_or_mark_failed` call sites in `src/awf/control/executor/execution_flow.py`.
2. Update each direct generic missing-HEAD handler to resolve the helper with `getattr(...)` and only invoke it when present.
3. Add or update a focused unit test that drives a minimal executor without the helper through the cleanup-failure path.
4. Run targeted pytest for the affected tests only.
5. Create `plans/PR614_CURRENT_HEAD_MISSING_HEAD_HELPER_VALIDATION.md` with evidence and any remaining gaps.
6. Commit the scoped fix locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure -q`
  - Passes and confirms the CI-reported regression does not recur.
- If code changes affect another nearby path, run the most focused added/nearby test for that path.
- Broad coverage and the full CI suite are intentionally not run locally; AWF/GitHub own those gates after agent completion.
