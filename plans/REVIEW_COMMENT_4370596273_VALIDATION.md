# Review comment #4370596273 validation

Plan reference: [REVIEW_COMMENT_4370596273_PLAN.md](./REVIEW_COMMENT_4370596273_PLAN.md)

## Requirement status

1. Replace hardcoded validation stale reason strings in staleness synchronization with shared constants.
   - Complete
   - Evidence: `src/awf/service/staleness.py` now uses
     `VALIDATION_INSUFFICIENT_TIER_STALE_REASON` and
     `VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON` via
     `_VALIDATION_STALE_REASONS_TO_PRESERVE` in `_mark_candidate_stale`.
2. Ensure stale reason checks do not re-create temporary literal sets each call.
   - Complete
   - Evidence: module-level `frozenset` constant
     `_VALIDATION_STALE_REASONS_TO_PRESERVE` introduced and reused.
3. Centralize stale reason label formatting in console helper.
   - Complete
   - Evidence: module-level `staleReasonLabels` map added in
     `apps/console/lib/merge-queue-format.ts` and used by
     `staleReasonLabel()` / `staleReasonDetail()`.
4. Keep behavior output-compatible while avoiding extra per-call allocation.
   - Complete
   - Evidence: label values remain stable for existing stale reason codes in current test fixture expectations.

## Verification commands executed
- Not run locally in this task per workspace policy; AWF/CI validation is authoritative post-agent.
