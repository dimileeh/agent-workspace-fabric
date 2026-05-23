# PR 283 Review Comment 4524755400 Plan

## Problem

Greptile's review-level PR comment identified two UX gaps in `awf init <path>`:
guided mode only accepts one validation command, and `--yes` is accepted without
`--write-profile` even though it only approves non-interactive profile writes.

## Scope

- Keep the fix limited to project-onboarding CLI behavior and focused unit tests.
- Do not change AWF branch, push, or broad validation ownership.
- Treat existing tests as policy evidence and preserve current onboarding behavior
  except where the review feedback identifies a gap.

## Requirements

- Reject `awf init <path> --yes` when `--write-profile` is not also supplied.
- Keep `awf init <path> --write-profile --yes` working.
- Let guided onboarding collect more than one validation command.
- Preserve the guided write confirmation as the final prompt.
- Record focused verification only; AWF/GitHub will run broad validation later.

## Implementation Steps

1. Add focused CLI regression tests for the invalid `--yes` combination and
   repeated guided validation-command input.
2. Run the new focused tests to confirm the current failure where practical.
3. Add the smallest CLI changes to reject orphan `--yes` and loop for additional
   guided validation commands.
4. Re-run the focused CLI tests and a narrow lint check for touched files.
5. Document validation evidence in a matching validation file.
