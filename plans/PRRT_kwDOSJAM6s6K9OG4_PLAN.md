# PRRT_kwDOSJAM6s6K9OG4 Plan

## Problem Statement and Scope

The review thread reports that repair start-head capture uses the open merge
candidate as a fallback when the repair worktree is missing, but not when the
worktree exists and either `rev-parse HEAD` fails or the parsed HEAD object is
missing from the mirror/worktree object database.

Scope is limited to `_repair_operation_start_head_result` and focused unit
coverage for those fallback paths.

## Requirements Checklist

- Confirm the current code does not already try the open merge candidate for
  existing-worktree start-head failures when no explicit fallback was passed.
- Add regression coverage for candidate fallback after `rev-parse HEAD` fails.
- Add regression coverage for candidate fallback after the primary HEAD object
  is missing.
- Preserve existing explicit `fallback_head_sha` behavior and failure behavior
  when no fallback is available.
- Run only targeted tests for the changed behavior; broad AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Add focused tests in the existing PR monitor runner coverage-edge test file.
2. Update `_repair_operation_start_head_result` to resolve either the explicit
   status fallback or the open merge candidate consistently across missing
   worktree, failed `rev-parse`, and missing primary-head-object paths.
3. Run the targeted unit tests covering the changed helper behavior.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K9OG4_VALIDATION.md`.
