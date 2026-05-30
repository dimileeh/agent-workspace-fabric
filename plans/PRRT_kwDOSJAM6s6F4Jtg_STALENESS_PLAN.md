# Review Comment 4395522190 Staleness Plan

## Problem Statement And Scope

Review-level comment `4395522190` points to inline thread
`PRRT_kwDOSJAM6s6F4Jtg`, which reports that staleness snapshots still use non-empty
`workspace.owned_paths` directly. When those workspace paths contain only
filtered AWF internal plan artifacts, staleness ignores real
`attempt.owned_paths`, while merge queue dependency ordering already falls back
to attempt paths in that case.

Scope is limited to staleness snapshot owned-path selection and focused
regression coverage for the review thread.

## Requirements Checklist

- Add a regression test proving a candidate with internal-plan-artifact-only
  workspace paths still gets blocking `STALE_OVERLAP` from real attempt-owned
  paths.
- Preserve advisory handling for real internal plan artifact overlaps.
- Keep merge queue behavior unchanged.
- Run only focused local checks for the changed behavior; AWF/GitHub own broad
  validation after agent completion.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add the regression test to the staleness refresh service unit tests.
2. Confirm the test fails against the current implementation when practical.
3. Update `src/awf/service/staleness.py` so `_snapshot_for` filters workspace
   owned paths with the shared internal-plan-artifact classifier and falls back
   to attempt-owned paths when the filtered workspace set is empty.
4. Run the focused regression test, then a nearby focused staleness test slice.
5. Create the validation document with requirement status and evidence.
6. Stage only changed files and commit locally with the review comment id.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py::<targeted-test> -q`
  - Passes after implementation and fails before implementation when practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py -q`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run in this agent phase.
