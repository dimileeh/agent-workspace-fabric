# PRRT_kwDOSJAM6s6F69J7 Plan

## Scope

Address the unresolved review thread reporting that comment-repair `needs_human`
verdict reasons are read from monitor state but never written.

## Steps

1. Add focused regression coverage proving an agent-returned `needs_human`
   reason is persisted for inline review threads and review-level comments.
2. Implement the smallest monitor-state write path for
   `__needs_human_reason__:<item_id>`, clearing stale reasons when a later
   verdict has no needs-human reason.
3. Run only targeted tests for the touched PR monitor runner behavior.
4. Record validation evidence and leave broad AWF/GitHub validation to the
   post-agent pipeline.
