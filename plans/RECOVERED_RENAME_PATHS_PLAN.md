# Recovered Rename Paths Plan

## Problem Statement and Scope

An unresolved PR review thread reports that `_commit_dirty_worktree` recovers a
missing HEAD object, then loads recovered changed paths with
`git diff --name-only -z`. Rename diffs may omit the source path, so later
ownership/protected-scope checks can operate on an incomplete path set.

Scope is limited to the missing-HEAD recovery path in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and its focused regression
coverage.

## Requirements Checklist

- Use a recovered committed diff format that includes rename source and
  destination paths.
- Preserve the existing agent-runtime path filtering and no-op behavior.
- Fail closed if the recovered committed diff output is malformed.
- Add focused regression coverage for rename source preservation.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests/checks.

## Implementation Steps

1. Add a failing unit test for recovered missing-HEAD rename diffs.
2. Change the recovered diff command from `--name-only` to `--name-status`.
3. Parse recovered paths with the existing `_changed_paths_from_name_status_z`
   helper and keep filtering runtime-only paths afterward.
4. Run the targeted unit test file or selected test cases covering the changed
   path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`

Pass criteria: the targeted runtime monitor tests pass, including the new
rename-source regression. Full AWF/GitHub validation remains managed by AWF
after agent completion.
