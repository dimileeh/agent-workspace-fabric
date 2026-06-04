# Review Thread PRRT_kwDOSJAM6s6HAd2j: GC Teardown Side Effects Plan

## Issue

The PR monitor completion path now delegates post-merge cleanup to
`run_workspace_filesystem_gc` with a compose teardown callback. If that callback
fails, path deletion is skipped, but the GC engine currently still revokes
secret leases and releases resource reservations for the candidate.

## Scope

- Verify the reported lifecycle and GC ordering in local code.
- Add a focused regression proving failed compose teardown preserves workspace
  leases and reservations.
- Change only the GC execution ordering needed to gate those side effects on
  compose teardown success or skip.
- Record focused validation evidence. Broad AWF/GitHub validation remains
  managed by AWF after this agent completes.

## Implementation Steps

1. Extend the existing monitor completion GC failure test with an active secret
   lease and resource reservation.
2. Run that focused test and confirm it fails on the current behavior.
3. Pre-run compose teardown inside the GC engine, compute the candidates whose
   cleanup can proceed, and revoke leases/release reservations only for those
   candidates.
4. Reuse the precomputed teardown results during path deletion so callbacks are
   not invoked twice and failed teardowns still produce skipped path outcomes.
5. Re-run the focused regression and adjacent compose-teardown GC tests.

## Expected Verdict

If the regression passes after the minimal GC change, commit the fix locally and
print `AWF-VERDICT: FIXED`.
