# Review 4578892384 Custom Plan Artifact Filter Plan

## Problem Statement and Scope

Greptile reported that `test_custom_plan_artifact_overlap_does_not_block_later_candidate`
passes vacuously because its two custom plan artifact paths are distinct concrete
files. The test should exercise a custom profile artifact owned-path overlap that
would block without `interworkspace_owned_paths` filtering, while keeping real
source owned paths non-overlapping.

Scope is limited to the regression test and this plan/validation documentation.

## Requirements Checklist

- [ ] Preserve existing merge-queue behavior expectations.
- [ ] Make the custom-profile merge-queue test fail if custom plan artifacts are
  not filtered before inter-workspace blocker comparison.
- [ ] Keep the scenario focused on non-overlapping source paths plus overlapping
  custom plan artifact paths.
- [ ] Run a focused test for the changed behavior only.
- [ ] Do not run broad AWF/GitHub-owned validation in the agent phase.

## Implementation Steps

1. Update the custom-profile merge-queue test data so both candidates own the
   same custom artifact glob path derived from the profile template.
2. Add a local assertion documenting that the unfiltered artifact path would
   overlap.
3. Run the focused pytest selection for the changed test.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py -q -k custom_plan_artifact`
  - Passes, proving the custom profile artifact overlap is ignored by merge
    queue blocker calculation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
