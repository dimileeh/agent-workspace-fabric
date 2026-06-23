# PRRT_kwDOSJAM6s6LqQs8 Plan

## Problem Statement and Scope

The review thread reports that two merge-attention tests assert the durable
post-lock marker is preserved, but do not assert that the in-memory
`MonitorState.threads_addressed_ids` marker remains consistent with the
persisted workspace marker. Scope is limited to verifying and, if valid, adding
the missing parity assertions in `tests/unit/runtime/test_pr_monitor_merge_attention.py`.

## Requirements Checklist

- Verify the cited tests still lack in-memory/durable marker parity assertions.
- Add only the minimal assertions needed to prove the in-memory marker survives
  post-lock preservation beside the durable marker.
- Preserve existing regression assertions and comments.
- Run focused validation for the affected tests only; leave broad AWF/GitHub
  validation to AWF after agent completion.
- Commit the fix locally with a conventional commit for the review thread.

## Implementation Steps

1. Inspect the cited line and the related second location.
2. Add `state.threads_addressed_ids` parity assertions after `persisted_raw` is
   confirmed present in both affected tests.
3. Run targeted pytest for the affected test file or specific affected tests.
4. Record validation evidence in the companion validation document.
5. Stage only changed files and commit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q -k 'post_lock_gate_preserves_blocked_marker_without_restamping or long_coordinator_wait_preserves_fresh_at_entry_attention_across_post_lock_queue_wait'`

Pass criteria: both targeted tests pass with the new parity assertions. Full
suite, coverage, and CI-equivalent validation are intentionally not run in the
agent phase because AWF owns broad validation after completion.
