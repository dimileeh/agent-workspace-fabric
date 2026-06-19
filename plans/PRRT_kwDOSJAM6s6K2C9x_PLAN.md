# PRRT_kwDOSJAM6s6K2C9x Plan

## Problem Statement and Scope

The pre-push validation fix pass recovers a missing HEAD object by passing the
pass-opening `fix_start_head` directly to filesystem recovery. If that commit is
not present in the mirror, recovery fails without falling back to the open
merge-candidate head. Scope is limited to the fix-pass missing-HEAD recovery
anchor selection and focused regression coverage.

## Requirements Checklist

- Verify the review claim against the current fix-pass code.
- Add a focused regression test that fails when the fix pass uses a stale
  `fix_start_head` instead of the open merge-candidate head.
- Reuse the existing mirror commit existence and open merge-candidate lookup
  helpers rather than adding a new recovery mechanism.
- Preserve existing protected-scope recovery behavior and reason-code handling.
- Run only targeted tests for the changed behavior; broad AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Add a regression test in the existing pre-push validation fix-pass test part.
2. Import the shared remote-repair helpers into the fix-pass module.
3. Before filesystem recovery, verify `fix_start_head` exists in the mirror when
   a mirror is available.
4. If the anchor is missing, log the stale anchor and fall back to
   `_open_merge_candidate_head_sha`.
5. Keep existing unrecoverable behavior when no usable anchor is available.

## Verification

- Run the new focused test and confirm it fails before implementation when
  practical.
- Run the focused changed test file or targeted test selection after the fix.
- Do not run full repository tests, full coverage, or CI-equivalent validation.
