# PRRT_kwDOSJAM6s6K2ayz Plan

## Problem Statement and Scope

The mirror hook-path repair path treats any `git config --unset-all core.hooksPath`
failure as terminal. Git returns exit 5 when the key has already been removed,
which can happen when a sibling workspace repairs the same shared mirror after
this workspace's initial probe.

Scope is limited to `repair_mirror_hooks_path` and focused unit coverage for the
concurrent cleanup case.

## Requirements Checklist

- Treat a concurrent removal of `core.hooksPath` as a successful repair only
  after verifying the key is now absent.
- Preserve terminal `MIRROR_HOOKS_PATH_REPAIR_FAILED` behavior for real unset
  failures.
- Add a regression test for the concurrent cleanup race.
- Run only focused validation for the changed behavior; broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a focused test that removes `core.hooksPath` between the initial probe and
   the repair command, causing the repair command to return Git's no-match exit.
2. Update `repair_mirror_hooks_path` to re-probe when unset returns exit 5 and
   return success only when the re-probe shows the key is absent.
3. Run the targeted unit tests for `TestRepairMirrorHooksPath`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k TestRepairMirrorHooksPath`
  passes.
