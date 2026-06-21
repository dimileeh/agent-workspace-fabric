# Post-Validation Mirror Hooks Repair Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K8jdX` reports that `_run_pre_push_validation`
repairs the linked-worktree mirror `core.hooksPath` before validation, but can
return after failed validation or validation cleanup without repairing mirror
state again. If validation commands poison `core.hooksPath`, sibling/future
workspaces can inherit the bad mirror state.

Scope is limited to PR monitor pre-push validation exit paths in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`.

## Requirements Checklist

- Add a post-validation mirror hooks repair after validation commands and
  cleanup have had a chance to mutate mirror config.
- Cover validation failure and cleanup failure returns before `_git_push_result`
  is reached.
- Fail closed with the existing mirror-hooks poisoned reason if the post-
  validation repair itself fails.
- Preserve existing pre-validation repair behavior.
- Add focused regression tests without running broad AWF/GitHub validation.

## Implementation Steps

1. Add a small helper in `pre_push_validation.py` for post-validation mirror
   hooks repair.
2. Call the helper on validation exit paths after `run_profile_phases` starts:
   compose cleanup exception, unexpected exception, validation cleanup failure,
   and final validation pass/fail result.
3. Add focused tests in `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
   proving a validation failure performs a second repair and that a failed
   second repair blocks the result with `MIRROR_HOOKS_PATH_POISONED`.
4. Run the targeted tests for the touched behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation after completion.
