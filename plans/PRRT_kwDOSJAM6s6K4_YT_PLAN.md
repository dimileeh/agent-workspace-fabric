# PRRT_kwDOSJAM6s6K4_YT Plan

## Problem Statement and Scope

The missing-HEAD recovery abort path resets the worktree after a failed recovery commit,
but a failed commit hook can leave staged additions as untracked files after
`git reset --hard`. The next monitor cycle can then report pre-existing dirt instead of
the original recovery failure. Scope is limited to the missing-HEAD recovery helper and
its focused unit tests.

## Requirements

- Verify the review thread against the current code.
- Preserve existing policy-block cleanup behavior.
- Clean only staged addition paths known to have been created by recovery when a
  recovery abort happens after staging.
- Add a focused regression test for commit failure after an added recovery file was
  staged.
- Run targeted tests only; broad AWF/GitHub validation remains owned by AWF after this
  agent phase.

## Implementation Steps

1. Update the abort cleanup helper to accept staged untracked cleanup paths.
2. After a successful reset, run `git clean -fd -- <paths>` for those staged additions.
3. Pass the computed staged-addition cleanup paths from the recovery commit failure path.
4. Extend the existing commit-failure unit test to cover an added file and assert the
   clean command.

## Verification

- Run the focused unit test covering missing-HEAD recovery commit failure.
- Pass criteria: the test passes and records both reset and clean commands.
