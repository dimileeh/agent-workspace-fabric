# Owned Path Prompt Prefetch Plan

## Problem Statement And Scope

PR monitor comment repair prompts currently load workspace `owned_paths` inside
each thread/review-comment prompt builder. A single fix cycle can address a
batch of review threads and review-level comments for the same immutable
workspace row, so this creates redundant database reads.

Scope is limited to the PR monitor comment fix-cycle prompt path. CI repair and
direct helper callers may continue using the existing workspace lookup helper.

## Requirements Checklist

- Fetch prompt `owned_paths` once per `_run_fix_cycle` invocation.
- Pass the fetched paths to every thread and review-comment prompt in that fix
  cycle.
- Preserve existing direct-call behavior for `_address_thread`,
  `_address_review_comment`, and `_address_review_comment_result`.
- Add a focused regression proving a multi-item fix cycle performs one owned
  path prompt load.
- Avoid broad AWF/GitHub-owned validation; run only targeted unit tests.

## Implementation Steps

1. Add optional `owned_paths` parameters to the comment prompt address helpers.
2. Load `owned_paths` once near the start of `_run_fix_cycle` after early dirty
   worktree/start-head exits.
3. Thread the loaded list through thread and review-comment addressing calls.
4. Add a unit test with multiple review threads/comments that counts owned-path
   prompt loads.
5. Run the focused regression test module or specific test node only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes, including the new prefetch regression.

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns
broad validation, provenance, logs, timeouts, and merge gating after completion.
