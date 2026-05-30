# PRRT_kwDOSJAM6s6F37co Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F37co_PLAN.md`

## Requirement Status

- Complete: Added a regression proving merge queue blocking falls back to
  attempt-owned paths when workspace-owned paths are present but filter down to
  no interworkspace paths.
- Complete: Preserved existing internal plan artifact behavior by keeping
  `interworkspace_owned_paths` filtering on both workspace and attempt paths.
- Complete: Kept changes scoped to merge queue owned-path selection, one focused
  test, and required plan/validation documents.
- Complete: Ran only focused local checks. Full AWF/GitHub validation,
  provenance, logs, and merge gating remain managed by AWF after agent
  completion.

## Evidence

Files changed:

- `src/awf/service/merge_queue.py`
- `tests/unit/service/test_merge_queue_ordering.py`
- `plans/PRRT_kwDOSJAM6s6F37co_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F37co_VALIDATION.md`

Red regression:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py::test_merge_queue_candidate_dependency_falls_back_when_workspace_paths_filter_empty -q`
  failed before the implementation because `_candidate_blocks_target` returned
  `False`.

Green checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_merge_queue_ordering.py -q`
  passed with `24 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/merge_queue.py tests/unit/service/test_merge_queue_ordering.py`
  passed.

## Gaps

No planned requirement gaps remain.
