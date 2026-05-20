# Merge Queue Head-Of-Line Validation

Plan: `plans/MERGE_QUEUE_HEAD_OF_LINE_PLAN.md`

## Requirements

- Missing `owned_paths` on either side must not create a queue blocker: Complete.
- Declared overlapping paths must still create a queue blocker: Complete.
- Declared disjoint paths must still not block: Complete.
- Tests must document the corrected missing-owned-path behavior: Complete.

## Evidence

- Changed `src/awf/service/merge_queue.py` so `_candidate_blocks_target` only
  blocks on explicit owned-path overlap.
- Updated `tests/unit/service/test_merge_queue_ordering.py` assertions and test
  name for missing-owned-path behavior.

## Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py -q
# 23 passed

uv run --python 3.12 --extra dev pytest tests/unit/api/test_merge_queue.py -q
# 40 passed

uv run --python 3.12 --extra dev ruff check src/awf/service/merge_queue.py tests/unit/service/test_merge_queue_ordering.py tests/unit/api/test_merge_queue.py
# All checks passed
```
