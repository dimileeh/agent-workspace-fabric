# Review 4578892384 Maintainability Plan

## Problem Statement And Scope

Address the review-level maintainability feedback from PR comment
`issue:4578892384` for the owned-path plan-artifact classifier and overlap
graph.

Scope is limited to:

- clarifying the configured artifact matching helper contract;
- documenting the intentional workspace-id glob pattern/value inversion;
- removing redundant overlap-graph deduplication without weakening duplicate
  path protection.

## Requirements Checklist

- Add focused regression coverage that `interworkspace_owned_paths()` returns
  deduplicated inter-workspace paths while preserving first-seen caller strings.
- Move duplicate filtering into the shared owned-path helper so callers receive
  stable unique output.
- Clarify `owned_paths.py` helper docstrings/comments around normalized inputs
  and configured workspace-id glob matching.
- Remove the outer `dict.fromkeys()` wrappers from
  `src/awf/service/overlap_graph.py`.
- Avoid broad AWF/GitHub-owned validation; run only focused unit tests and
  targeted lint for changed files.

## Implementation Steps

1. Update `tests/unit/common/test_owned_paths.py` with a failing regression for
   duplicate non-artifact paths.
2. Run the focused test or test node and confirm the regression fails when
   practical.
3. Update `src/awf/common/owned_paths.py` to deduplicate filtered output and add
   clarifying docstrings/comments.
4. Simplify `src/awf/service/overlap_graph.py` to sort the helper output
   directly.
5. Re-run focused tests and targeted lint for the changed files.
6. Record evidence in
   `plans/REVIEW_4578892384_MAINTAINABILITY_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_interworkspace_owned_paths_deduplicates_filtered_paths -q`
  initially fails before the implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/service/test_overlap_graph.py -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py src/awf/service/overlap_graph.py tests/unit/common/test_owned_paths.py`
  passes after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
