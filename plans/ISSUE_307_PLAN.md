# Issue 307 Remonitor Hint Freeze Plan

## Goal

Fix `POST /v1/workspaces/{id}/remonitor` so an operator-provided `reason` is
actionable when a monitored PR is already past the non-check reviewer settle
window. The monitor must not auto-merge before the hint is processed by a repair
pass.

## Implementation Steps

1. Add failing regression tests first:
   - pure monitor decision blocks merge while a pending operator hint exists;
   - remonitor before settle preserves existing no-warning behavior;
   - remonitor after settle persists the hint, re-arms grace/freeze state, and
     returns `REMONITOR_PAST_SETTLE`;
   - runner prompt/repair path includes the operator hint and clears it only
     after a repair pass is handled.
2. Persist pending operator-hint metadata in the existing monitor state JSON
   using namespaced helper keys, avoiding a migration unless the existing state
   shape proves insufficient.
3. Add monitor-state helpers for reading, writing, freezing, and clearing
   pending operator hints. Reuse existing initial-review/non-check settle timing
   machinery rather than adding a separate merge timer.
4. Update `remonitor_workspace()` to store non-empty reasons for monitored PRs,
   detect past-settle state, reset monitor claims, arm the grace cooldown, emit
   reason-coded events, and include response warnings when appropriate.
5. Wire the PR monitor runner so pending hints dispatch a repair pass with the
   requested prompt addendum, block auto-merge while pending, and become
   merge-eligible only after the repair pass marks the hint processed and gates
   are re-evaluated.
6. Add response-warning schema support and regenerate `openapi.json` if the
   public response schema changes.
7. Write `plans/ISSUE_307_VALIDATION.md` with focused check results and any
   deferred stretch work. Broad AWF/GitHub validation remains owned by AWF after
   this agent phase unless explicitly re-authorized.

## Non-Goals

- Do not implement `block_auto_merge_until`.
- Do not replay historical merge attempts.
- Do not add merge-imminent notifications or per-thread acknowledgement APIs.
- Do not switch branches, push, rebase, or open a PR.
