# PRRT_kwDOSJAM6s6K-eeS Plan

## Scope

Address the PR review thread reporting that agent cleanup failures can be
overwritten by missing-HEAD recovery verification failures in
`src/awf/control/executor/execution_flow.py`.

## Steps

1. Add a focused regression in the executor cleanup recovery tests for the
   case where agent cleanup hits missing HEAD, HEAD recovery succeeds, and the
   post-agent verification helper would otherwise mark the workspace failed
   with `GIT_OBJECT_MISSING`.
2. Update the agent cleanup missing-HEAD path so cleanup failure remains the
   terminal reason after successful HEAD recovery.
3. Run the narrow regression test(s) touched by this change only. Full
   AWF/GitHub validation is managed after agent completion.
4. Record validation evidence in the matching validation document.
