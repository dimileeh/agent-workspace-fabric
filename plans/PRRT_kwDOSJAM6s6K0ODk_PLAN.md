# PRRT_kwDOSJAM6s6K0ODk Plan

## Scope

Resolve the inline review thread reporting that
`tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py`
exceeds the first-party file line limit after a helper was added.

## Steps

1. Verify the current line count and inspect the pointed code.
2. Move the smallest coherent regression block out of part 001 into a new
   executor coverage shard, keeping existing assertions unchanged.
3. Run focused checks:
   - direct line-count check for the affected shards;
   - the moved test;
   - the maintainability line-limit test if it does not force unrelated broad
     validation ownership.
4. Record validation evidence and commit the scoped change.

## Non-goals

- Do not address unrelated oversized shards covered by separate review threads.
- Do not run broad AWF/GitHub-owned validation.
