# Review comment #4370596273 execution plan

## Problem and scope
Address PR review feedback in `dimileeh/aira-agent-workspace-fabric#288` regarding:
- avoiding hardcoded stale-reason strings when synchronizing candidate stale reasons in service logic
- moving stale-reason label formatting to a module-level constant in console formatting helper

## Requirements
- [ ] Replace hardcoded validation stale reason strings in staleness synchronization with shared constants.
- [ ] Ensure stale reason checks do not re-create temporary literal sets each call.
- [ ] Centralize stale reason label formatting in `apps/console/lib/merge-queue-format.ts` with module-level mapping.
- [ ] Keep behavior output-compatible for existing callers while avoiding extra per-call allocation.

## Implementation steps
1. Update `src/awf/service/staleness.py`:
   - import validation stale-reason constants from `awf.runtime.merge_eligibility`.
   - define a module-level constant set for preserved validation stale reasons.
   - use that constant in `_mark_candidate_stale` when checking existing stale reasons.
2. Update `apps/console/lib/merge-queue-format.ts`:
   - add module-level stale reason label mapping.
   - use the mapping in `staleReasonLabel()` and `staleReasonDetail()`.

## Verification commands
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py -q`
- `node --test apps/console/lib/merge-queue-format.test.mjs`

## Pass criteria
- No hardcoded stale-reason validation strings remain in `_mark_candidate_stale` path.
- Stale reason label formatting uses module-level mapping.
- Existing focused unit tests remain semantically passing.
