# PR614 Review Thread PRRT_kwDOSJAM6s6K66zF Plan

## Problem Statement And Scope

The reviewer reports that `repair_mirror_hooks_path()` preserves the allowlisted
relative `core.hooksPath` value `.githooks/Lefthook` without checking whether an
attached workspace actually provides that hook directory. This can leave a
shared mirror configured to bypass `.git/hooks` for future commits.

Scope is limited to `src/awf/node/git_manager.py` hook-path cleanup behavior and
focused unit coverage in `tests/unit/node/test_git_manager.py`.

## Requirements Checklist

- Reproduce the unsafe case with a focused regression test: a mirror configured
  with `.githooks/Lefthook` and no attached worktree hook directory must be
  repaired.
- Preserve `.githooks/Lefthook` only when the mirror has registered worktree
  evidence that the relative hook directory exists.
- Keep existing poison-path cleanup behavior unchanged.
- Run only targeted tests for the changed behavior; leave broad AWF/GitHub
  validation to AWF after agent completion.

## Implementation Steps

1. Update the existing legitimate hook-path unit test to build an attached
   worktree containing `.githooks/Lefthook`.
2. Add a regression test for an attached worktree missing `.githooks/Lefthook`.
3. Teach mirror hook-path repair to validate allowlisted relative hook paths
   against registered worktree roots before preserving them.
4. Run the focused unit tests around `TestRepairMirrorHooksPath`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k TestRepairMirrorHooksPath`
  - Passes all focused hook-path repair tests.
  - Confirms broad validation was not run locally.
