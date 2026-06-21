# PRRT_kwDOSJAM6s6K-_X2 Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K-_X2` reports that mirror hook repair can fail when one included config file exposes more than one `core.hooksPath` value. `_repair_hooks_path_config` removes the matching include for the first value, then processes another value from the same origin and raises because the include is already gone.

Scope is limited to `src/awf/node/git_manager.py`, the focused mirror hook repair tests, and this plan/validation record.

## Requirements Checklist

- Add a focused regression test for an included config with multiple `core.hooksPath` values from the same origin.
- Keep the repair behavior strict for unmapped or unremovable included origins.
- Avoid reprocessing an included origin after its include has already been removed.
- Run only focused validation for the changed behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
2. Run the focused test to confirm the current failure when practical.
3. Track repaired included origins in `_repair_hooks_path_config` and skip repeated values from the same origin.
4. Re-run the focused test and a nearby mirror hook repair test target.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q -k "multiple_hooks_paths_from_same_included_origin or removes_include_exposing_poisoned_hooks_path"`
  - Passes with the new regression and an existing include repair test.
- Full AWF/GitHub validation is intentionally not run in this agent phase per workspace contract.
