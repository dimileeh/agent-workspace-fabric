# Merge Attention Queue Wait TTL Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6LqhqC` reports that queue waits preserve an
old `merge_block_attention` marker when forge status still reports branch
protection active, but merge critical-section entry still applies the marker TTL
alone. After a long queue wait, the aged marker can be treated as resolved and
clear `awaiting_human_since` even though branch protection is still active.

Scope is limited to `src/awf/runtime/pr_monitor_runner/**`, focused unit tests,
and required plan/validation documents for this thread.

## Requirements Checklist

- [ ] Verify the review claim against current code.
- [ ] Add a focused regression proving an active forge mergeability signal
      preserves merge-block attention even when the marker is TTL-stale at
      critical-section entry.
- [ ] Keep existing stale-marker cleanup behavior for resolved or unproven
      branch-protection blocks.
- [ ] Keep the fix minimal and scoped to merge attention / merge loop.
- [ ] Run focused tests only; full AWF/GitHub validation remains managed by AWF
      after agent completion.
- [ ] Commit the fix locally with a conventional commit message referencing the
      review thread.

## Implementation Steps

1. Inspect `merge_attention.py` and `merge_loop.py` around queue-wait preserve
   and critical-section entry.
2. Write the failing regression in `tests/unit/runtime/test_pr_monitor_merge_attention.py`.
3. Pass the current `status` and forge into `_clear_stale_merge_attention` from
   merge critical-section entry.
4. In `_clear_stale_merge_attention`, preserve and re-stamp the marker when the
   forge verdict is still `active`, even if the TTL says stale.
5. Run the focused merge-attention unit test file or selected tests that cover
   the changed behavior.
6. Write validation evidence in `plans/MERGE_ATTENTION_QUEUE_WAIT_TTL_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Pass criterion: all merge-attention focused tests pass.

Full repository validation, coverage gates, and CI-equivalent checks are not run
inside this agent phase per the AWF workspace contract.
