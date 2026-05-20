# Merge Queue Head-Of-Line Plan

## Problem

Live AWF merge-queue data shows clean PR monitors waiting behind PR #264. The
queue policy treats missing `owned_paths` as universal overlap, so adopted PR
monitors without declared ownership block each other even when there is no
declared coordination evidence.

## Scope

- Keep the change narrow to `src/awf/service/merge_queue.py` and its tests.
- Preserve blocking for declared overlapping `owned_paths`.
- Preserve monitor-owned recovery blockers.
- Do not change GitHub merge mechanics or PR monitor validation behavior.

## Requirements

- Missing `owned_paths` on either side must not create a queue blocker.
- Declared overlapping paths must still create a queue blocker.
- Declared disjoint paths must still not block.
- Tests must document the corrected missing-owned-path behavior.

## Implementation Steps

1. Update `_candidate_blocks_target` so only explicit path overlap blocks.
2. Update merge queue ordering tests that encoded the old conservative fallback.
3. Run focused merge queue tests.

## Verification

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_merge_queue.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/merge_queue.py tests/unit/service/test_merge_queue_ordering.py tests/unit/api/test_merge_queue.py
```
