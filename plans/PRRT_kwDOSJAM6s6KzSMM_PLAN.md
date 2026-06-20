# PRRT_kwDOSJAM6s6KzSMM Plan

## Scope

Address the inline review thread reporting that pre-push validation reuses an
unresolvable `rev-parse HEAD` SHA as the missing-HEAD recovery anchor.

## Steps

1. Verify the reported line against the local implementation and compare it
   with the dirty-worktree commit recovery path.
2. Add a focused regression test proving `_run_pre_push_validation` uses the
   open merge-candidate PR head when the HEAD object is missing and no
   operation-start anchor is available.
3. Update the pre-push recovery anchor selection to match the existing
   `_commit_dirty_worktree` fallback contract.
4. Run the narrow targeted test(s) for the changed behavior only. Full AWF and
   GitHub validation remains owned by AWF after agent completion.
5. Record validation results in the matching validation document and commit the
   scoped fix.
