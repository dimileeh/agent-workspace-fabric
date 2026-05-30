# PRRT_kwDOSJAM6s6FxHYU Lock Overlap Prefilter Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6FxHYU` reports that
`_workspace_overlap_risks_by_id` repeatedly calls `interworkspace_owned_paths`
for the same overlap candidate while iterating page workspaces. The scope is
limited to the in-memory overlap-risk calculation in `src/awf/service/locks.py`
and focused regression coverage in `tests/unit/service/test_locks.py`.

## Requirements Checklist

- Preserve existing overlap-risk results and ordering.
- Normalize each overlap candidate's interworkspace paths at most once per
  `_workspace_overlap_risks_by_id` call.
- Keep workspace-owned path normalization once per listed workspace.
- Avoid broad validation; AWF/GitHub own full validation after agent completion.

## Implementation Steps

1. Add a focused unit test that counts candidate path normalization calls and
   fails on redundant candidate normalization.
2. Precompute normalized candidate owned paths while grouping candidates by
   repository and base branch.
3. Re-run the focused unit test and a narrow service lock test module check.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py::test_overlap_risks_prefilters_candidate_owned_paths_once -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py -q`
  passes.
