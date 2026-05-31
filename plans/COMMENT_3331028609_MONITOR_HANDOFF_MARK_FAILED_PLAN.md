# Comment 3331028609 Monitor Handoff Mark Failed Plan

## Context

PR review thread `PRRT_kwDOSJAM6s6F-eyi` reports that monitor handoff setup
command failures can be swallowed if the detailed `_mark_failed` call raises.
The helper logs the persistence error and returns `False`, so handoff callers
can stop without any terminal workspace transition.

## Plan

1. Update the focused monitor handoff setup regression test so a failed
   detailed `_mark_failed` call must be followed by a fallback failure
   transition.
2. Run the focused test and confirm it fails against the current behavior.
3. Implement the smallest change in
   `src/awf/control/executor/monitor_handoff_setup.py`: if the detailed setup
   command failure persistence raises, log it, then try a generic monitor
   setup failure transition without dependency details. Do not swallow the
   fallback transition error if that also fails.
4. Run only focused unit coverage for the changed behavior. Full AWF/GitHub
   validation remains owned by AWF after agent completion.
5. Record validation evidence in the matching validation artifact and commit
   the scoped changes locally.
