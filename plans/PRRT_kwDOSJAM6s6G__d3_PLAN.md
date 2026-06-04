# PRRT_kwDOSJAM6s6G__d3 Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/control/worker/cleanup.py`
that points out duplicated classified-orphan reaper rescheduling logic in
`_maybe_reap_classified_orphans`.

Scope is limited to preserving the existing behavior while removing the
duplicated interval calculation and cursor update.

## Requirements Checklist

- Verify the review claim against the current code.
- Keep the reaper gated by configured cleanup and scan timing.
- Preserve success, transient database failure, and fatal failure rescheduling.
- Preserve existing logging behavior and reason codes.
- Run focused tests for the classified-orphan worker loop only.

## Implementation Steps

1. Inspect the target function and existing focused tests.
2. Remove the early return after exception logging so control reaches a single
   reschedule block.
3. Re-run the focused worker-loop tests.
4. Record validation evidence in the matching validation document.

## Verification

Run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_046.py -q
```

Pass criteria: the focused classified-orphan worker-loop tests pass. Full
AWF/GitHub validation remains owned by AWF after agent completion.
