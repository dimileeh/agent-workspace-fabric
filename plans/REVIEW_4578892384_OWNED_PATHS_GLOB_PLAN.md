# Review 4578892384 Owned Paths Glob Plan

## Problem Statement And Scope

Address the Greptile review-level feedback for PR #313 about
`src/awf/common/owned_paths.py`:

- Make the `/**` exact-match-only design explicit near the branch that skips
  sub-path matching.
- Keep `_workspace_id_glob_path_matches()` synchronized with
  `_WORKSPACE_ID_GLOB` by deriving the accepted literal prefix from the glob.

Scope is limited to the owned-path classifier, its focused unit tests, and this
plan/validation record.

## Requirements Checklist

- Add a focused regression test proving workspace-id matching follows a changed
  `_WORKSPACE_ID_GLOB` prefix.
- Update `_workspace_id_glob_path_matches()` to derive the regex prefix from
  `_WORKSPACE_ID_GLOB`.
- Add the requested inline comment documenting that standalone `/**` entries do
  not perform recursive sub-path matching.
- Run only targeted checks for the changed owned-path helper.
- Do not run AWF/GitHub-owned broad validation.

## Implementation Steps

1. Add a focused unit test in `tests/unit/common/test_owned_paths.py` that
   monkeypatches `_WORKSPACE_ID_GLOB` to a different prefix and verifies
   matching behavior through the public classifier.
2. Confirm the new test fails before implementation when practical.
3. Patch `src/awf/common/owned_paths.py` with the inline comment and derived
   prefix logic.
4. Re-run the focused test file.
5. Record validation evidence in
   `plans/REVIEW_4578892384_OWNED_PATHS_GLOB_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  must pass after the implementation.
- Full AWF/GitHub validation is intentionally not run during the agent phase;
  AWF owns broad validation, provenance, and merge gating after completion.
