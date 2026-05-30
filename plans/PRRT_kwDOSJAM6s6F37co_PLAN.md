# PRRT_kwDOSJAM6s6F37co Plan

## Problem Statement And Scope

The PR review thread reports that merge queue path blocking treats a non-empty
`workspace.owned_paths` list as authoritative before filtering internal AWF plan
artifact paths. If filtering removes every workspace path, the merge queue does
not fall back to `attempt.owned_paths`, so real attempt-only overlaps can be
missed.

Scope is limited to merge queue candidate owned-path selection and a focused
regression test.

## Requirements Checklist

- Add a regression proving merge queue blocking falls back to attempt-owned
  paths when workspace-owned paths are present but filter down to no
  interworkspace paths.
- Preserve existing behavior where internal plan artifact-only paths do not
  create queue blockers.
- Keep changes scoped to merge queue behavior and tests.
- Run only focused local checks; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Extend focused merge queue ordering tests with a candidate whose workspace
   paths contain only filtered internal plan artifacts and whose attempt paths
   contain a real overlapping source path.
2. Confirm the new regression fails on the current implementation when
   practical.
3. Update `_candidate_owned_paths` to filter workspace paths first, return them
   when any remain, and otherwise filter/fall back to attempt paths.
4. Run the focused affected test file or targeted test selections.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py -q`
  passes.
- If the initial red regression is run separately, it should fail before the
  implementation and pass after the implementation.
