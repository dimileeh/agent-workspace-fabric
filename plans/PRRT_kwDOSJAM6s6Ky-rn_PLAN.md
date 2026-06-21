# PRRT_kwDOSJAM6s6Ky-rn Plan

## Problem Statement And Scope

The pre-push validation fix pass handles missing-HEAD recovery by committing the
recovered filesystem tree, then synthesizing dirty porcelain rows and passing
them to the dirty-worktree protected-scope repair path. That path compares
`HEAD:path` to the worktree, which is incorrect after recovery already committed
the tree. Validate the committed recovery range `fix_start_head..recovered` with
the committed protected-file diff path instead.

Scope is limited to `pre_push_validation_fix_pass.py`, focused unit coverage,
and this plan/validation documentation.

## Requirements Checklist

- Detect protected-scope violations in recovered missing-HEAD fix-pass commits
  using committed diffs from `fix_start_head` to the recovered `HEAD`.
- Do not rely on synthetic dirty porcelain for recovered committed trees.
- Fail closed and roll back to `fix_start_head` when the recovered commit
  contains unowned protected-scope violations or committed diff evidence is
  unavailable.
- Preserve the clean/benign recovered-commit path.
- Add focused regression coverage for the review thread.

## Implementation Steps

1. Add a narrow helper in the fix-pass module to load owned paths, build
   committed protected-file diffs for recovered changed paths, and classify
   violations.
2. Replace the synthetic dirty-status repair call for recovered commits with
   the committed-range validator.
3. Update the existing recovered missing-HEAD regression and add a violation
   regression.
4. Run targeted tests for the touched unit file only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::test_pre_push_validation_fix_pass_validates_protected_scope_after_missing_head_recovery tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::test_pre_push_validation_fix_pass_blocks_recovered_commit_protected_scope_violations -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`
  must pass.
- Full AWF/GitHub validation is managed by AWF after agent completion and is not
  run in this workspace phase.

## Assumptions/Changes

- Validation is narrowed to the two affected regressions rather than the whole
  split test file. Running the full split file in this workspace surfaced
  unrelated mirror-hooks fake-command queue failures before the changed
  fix-pass logic; AWF/GitHub owns broader validation after agent completion.
