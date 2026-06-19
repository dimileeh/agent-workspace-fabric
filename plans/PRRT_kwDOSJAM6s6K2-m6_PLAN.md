# PRRT_kwDOSJAM6s6K2-m6 Plan

## Problem Statement and Scope

The missing-HEAD filesystem recovery path stages recovered files with `git add -A`.
When supply-chain policy blocks that recovery, the code resets tracked/index state
with `git reset --hard` but does not remove newly created untracked files. Those
leftovers can make the next monitor cycle report pre-existing dirt instead of the
original policy-blocked reason.

Scope is limited to the policy-blocked abort path in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and a focused regression test.

## Requirements Checklist

- [ ] Detect untracked paths in the staged recovery candidate before policy refresh.
- [ ] On policy-blocked recovery, reset tracked/index state and remove those untracked
  recovery paths with a literal pathspec clean.
- [ ] Preserve policy-blocked error propagation and existing cleanup logging behavior.
- [ ] Do not clean runtime-excluded paths that were unstaged before policy refresh.
- [ ] Add a focused regression test for policy-blocked cleanup of untracked recovery files.

## Implementation Steps

1. Add a focused failing test in the existing missing-HEAD recovery test module.
2. Update the recovery helper to remember candidate untracked paths from the staged
   name-status output after runtime exclusions.
3. In the policy-blocked path, run the existing hard reset, then a targeted
   `git clean -fd -- <paths>` when there are candidate untracked paths.
4. Keep warnings concise and aligned with the existing cleanup warning pattern.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`

Pass criteria: the focused test file passes. Full AWF/GitHub validation is managed
by AWF after agent completion per workspace contract.
