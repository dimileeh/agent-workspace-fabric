# PRRT_kwDOSJAM6s6K1FZE Porcelain Arrow Plan

## Problem Statement and Scope

The review thread reports that `src/awf/runtime/planning.py` splits any porcelain
path containing ` -> ` when the quote-aware rename parser returns `None`. A
literal arrow inside a quoted path should remain part of the path instead of
being treated as a rename separator.

Scope is limited to the planning porcelain parser and a focused regression test.

## Requirements Checklist

- Preserve quoted non-rename paths that contain a literal ` -> `.
- Continue selecting the destination path for real rename/copy porcelain records.
- Keep the change minimal and avoid unrelated parser refactors.
- Verify with focused planning parser tests only; broad AWF/GitHub validation is
  managed after agent completion.

## Implementation Steps

1. Add a failing regression test for a quoted modified path containing ` -> `.
2. Update `changed_paths_from_porcelain()` to split only when
   `split_porcelain_rename_paths()` finds an actual separator outside quotes.
3. Run the targeted planning parser tests.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning_parts/test_planning_part_001.py -q`

Pass criteria: the new regression and existing planning parser tests pass.
