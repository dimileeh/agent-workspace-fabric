# PRRT_kwDOSJAM6s6K5vVm Plan

## Problem Statement and Scope

The missing-HEAD recovery path runs protected-scope repair, then checks
post-repair dirtiness with `git status --porcelain` without using
`git_env_without_object_lookup_overrides()`. That leaves the status check
susceptible to poisoned Git object lookup environment variables and can alter
whether repair residue is committed.

Scope is limited to the post-protected-repair status call in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and a focused regression
test for that call.

## Requirements Checklist

- Confirm the review claim against the current code.
- Add or update a focused regression test before the production fix when
  practical.
- Pass `git_env_without_object_lookup_overrides()` to the post-repair
  `git status --porcelain --untracked-files=all` call.
- Run only targeted validation for the changed behavior; full AWF/GitHub
  validation remains managed after agent completion.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Inspect the referenced code and existing missing-HEAD recovery tests.
2. Extend the existing recovered-repair-status test to assert the post-repair
   status call receives sanitized Git env.
3. Run that single test and confirm it fails before the production change.
4. Add the `env=git_env_without_object_lookup_overrides()` argument to the
   post-repair status call.
5. Re-run the targeted test and any narrow lint/type check needed for touched
   files.
6. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_commit_dirty_worktree_returns_false_when_recovered_repair_status_fails -q`
  - Passes after the fix and fails before it because the status call env is
    missing.
- Optional focused lint if available and quick:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py`
  - Passes for touched files.
