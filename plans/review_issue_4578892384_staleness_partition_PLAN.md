# Review Issue 4578892384 Staleness Partition Plan

## Problem Statement And Scope

Address the review-level PR comment that `evaluate_staleness` partitions owned-path overlap with two passes, causing duplicate `_is_plan_artifact_path` calls for each overlapping path. Keep behavior unchanged: plan artifact overlaps remain advisory, real path overlaps remain blocking, and existing raw overlap matching semantics are preserved.

## Requirements Checklist

- [ ] Replace the two overlap list comprehensions with a single partition loop.
- [ ] Preserve ordering and existing finding behavior for advisory and blocking overlaps.
- [ ] Do not weaken or rewrite existing regression tests.
- [ ] Run focused validation for the staleness behavior touched here.

## Implementation Steps

1. Update `src/awf/service/staleness.py` so `plan_artifact_overlap` and `blocking_overlap` are built in one loop over `overlap`.
2. Leave existing tests intact unless the refactor exposes a real regression.
3. Run targeted staleness tests that cover plan-artifact-only, README-as-real-doc, and mixed artifact/source overlap behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_plan_artifact_only_overlap_is_advisory_without_target_advanced tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_mixed_plan_artifact_and_source_overlap_blocks_on_source -q`
  - Passes with no failures.

Full AWF/GitHub validation is intentionally left to AWF after agent completion per the workspace contract.
