# PR614 Repair Start HEAD CI Fix Plan

## Problem Statement and Scope

PR #614 CI is failing in `python-coverage-shards (5)` and `python-coverage-shards (6)`.
The failing tests unexpectedly receive `REPAIR_START_HEAD_UNAVAILABLE` before the
PR monitor fix-cycle logic reaches its intended comment, retry, push, or merge
paths. The fix is scoped to the repair-operation start HEAD capture path and
the focused tests needed to prove that existing monitor behaviors are preserved.

## Assumptions/Changes

- After the first fix and commit, the completed GitHub Actions run showed
  `python-coverage-shards (1)` also failing with the same
  `REPAIR_START_HEAD_UNAVAILABLE` root cause in integration-runtime monitor
  tests. The implementation scope remains unchanged; focused validation now
  includes representative shard 1 integration files as well.

## Requirements Checklist

- Diagnose the exact guard that is short-circuiting shard 5 and shard 6 tests.
- Preserve the new protected-scope behavior: do not start agent repair work when
  the operation start HEAD cannot be captured or safely inferred.
- Restore existing PR monitor fix-cycle behavior for tests that already provide
  a usable PR/status/candidate head fallback.
- Add or update focused regression coverage for the fixed behavior.
- Run focused shard-relevant tests only; full AWF/GitHub validation remains
  managed by AWF after agent completion.
- Do not modify protected workflow or quality-gate configuration.

## Implementation Steps

1. Inspect the remote repair start HEAD capture implementation and its callers.
2. Reproduce one representative failing shard 5 or shard 6 test locally.
3. Implement the smallest change that allows a valid fallback HEAD to seed the
   operation baseline when direct local capture is unavailable.
4. Run focused tests around remote repair fallback capture and representative
   failed PR monitor paths.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused failing tests> -q`
  passes for representative shard 1, shard 5, and shard 6 failures.
- Additional targeted test files around remote repair start HEAD fallback pass
  if touched by the fix.
- No broad coverage gate, full unit suite, or CI-equivalent validation is run
  locally; AWF owns that after this agent phase.
