# Merge Attention Clock Seam Plan

## Problem Statement

Fix #675 and #677 in the merge-block attention path. The fresh-at-entry
regressions must use a deterministic clock so they fail against post-wait
wall-clock classification, and queue/reviewer/grace waits must not clear a
merge-rejection-origin marker merely because GitHub reports `CLEAN`.

## Requirements Checklist

- Add one runner now-provider seam, defaulting to `datetime.now(UTC)`.
- Use the seam for merge critical-section entry, merge-block marker stamps,
  freshness checks, durable re-stamps, and workspace attention timestamps in the
  touched merge-attention path.
- Tighten both fresh-at-entry regressions with a small TTL and fake clock.
- Add a regression for GitHub `CLEAN` preserving merge-rejection-origin
  attention.
- Preserve the GitHub-vs-Bitbucket `CLEAN` asymmetry for non-rejection
  observable resolution.
- Keep changes scoped to merge-attention timing/clock logic and focused tests.
- Run only focused validation; broad AWF/GitHub validation remains post-agent.

## Implementation Steps

1. Add the now-provider dependency and route existing `datetime.now(UTC)` reads
   in `merge_loop.py` and `merge_attention.py` through it.
2. Update merge-rejection marker stamping to pass the injected time.
3. Harden queue-wait clearing so GitHub `CLEAN` resolves ordinary markers but
   preserves merge-rejection-origin attention.
4. Replace real-sleep TTL regressions with fake-clock coordinator advances.
5. Add the #677 regression and revise the #671 asymmetry test minimally.
6. Run the targeted merge-attention test file and focused lint/type checks if
   signatures changed.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/merge_loop.py src/awf/runtime/pr_monitor_runner/runner.py src/awf/runtime/pr_monitor_runner/types.py tests/unit/runtime/test_pr_monitor_merge_attention.py`

Full suite, coverage gates, and CI-equivalent validation are intentionally left
to AWF after completion.
