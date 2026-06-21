# PRRT_kwDOSJAM6s6K-dP_ Plan

## Problem Statement and Scope

The PR review thread reports that recovery-anchor validation accepts commit
objects that are only reachable through a mirror repository alternates file.
The scope is limited to the mirror anchor existence helper used by PR monitor
missing-HEAD recovery paths.

## Requirements Checklist

- Add a focused regression test showing mirror repository alternates cause
  `_mirror_commit_object_exists` to fail closed.
- Keep environment object lookup override stripping intact for the normal
  `cat-file` probe.
- Make the implementation minimal and local to PR monitor mirror anchor
  validation.
- Do not run broad AWF or CI-equivalent validation; record focused checks only.

## Implementation Steps

1. Add a unit test in the existing PR monitor runner test area for mirror
   alternates rejection.
2. Run that test and confirm it fails against the current helper.
3. Update `_mirror_commit_object_exists` to reject a declared mirror
   `objects/info/alternates` file before invoking `git cat-file`.
4. Run the focused unit test and any directly impacted nearby test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k mirror_commit_object_exists`
  passes.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per workspace contract.
