# PRRT_kwDOSJAM6s6K9S74 Plan

## Problem Statement and Scope

An unresolved PR review thread reports that dirty-worktree missing-HEAD recovery
can re-anchor to the open merge-candidate SHA in worktrees without a linked
mirror before verifying that the captured `operation_start_head` commit still
exists. The mirror-backed branch verifies the operation-start anchor first; the
no-mirror branch should follow the same priority so recovery, protected-scope
rollback, and filesystem reconstruction do not run from the wrong commit.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair.py` and
focused unit coverage in the existing PR monitor runner part test file.

## Requirements Checklist

- Add a regression test that fails before implementation by proving the
  no-mirror missing-HEAD branch checks and uses a valid `operation_start_head`
  before consulting a differing merge-candidate SHA.
- Update no-mirror recovery anchor selection to verify `operation_start_head`
  first and fall back to the merge candidate only when the preferred anchor is
  absent or dangling.
- Preserve existing candidate fallback and unrecoverable behavior.
- Keep changes minimal and avoid protected workflow/config changes.
- Run targeted tests only; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a focused unit regression beside the existing missing-HEAD recovery tests.
2. Run the new test to confirm it fails against the current no-mirror branch.
3. Update the no-mirror branch in `_commit_dirty_worktree` to verify
   `operation_start_head` before candidate fallback.
4. Run the new test and nearby missing-HEAD recovery tests.
5. Record validation evidence in `PRRT_kwDOSJAM6s6K9S74_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_no_mirror_prefers_verified_operation_start_before_candidate -q`
  - Fails before implementation because candidate fallback is used first.
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -k "missing_head" -q`
  - Existing missing-HEAD behavior remains green.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  - Changed files satisfy lint.
