# PRRT_kwDOSJAM6s6FxHYU Lock Overlap Prefilter Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FxHYU_LOCK_OVERLAP_PREFILTER_PLAN.md`

## Requirement Status

- Preserve existing overlap-risk results and ordering: Complete.
- Normalize each overlap candidate's interworkspace paths at most once per
  `_workspace_overlap_risks_by_id` call: Complete.
- Keep workspace-owned path normalization once per listed workspace: Complete.
- Avoid broad validation; AWF/GitHub own full validation after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/service/locks.py`
- `tests/unit/service/test_locks.py`
- `plans/PRRT_kwDOSJAM6s6FxHYU_LOCK_OVERLAP_PREFILTER_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FxHYU_LOCK_OVERLAP_PREFILTER_VALIDATION.md`

Focused checks:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py::test_overlap_risks_prefilters_candidate_owned_paths_once -q`
  failed with `assert 2 == 1`, confirming redundant candidate normalization.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py::test_overlap_risks_prefilters_candidate_owned_paths_once -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py -q`
  passed with `10 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/locks.py tests/unit/service/test_locks.py`
  passed.

Note: a direct `ruff check ...` invocation was unavailable because `ruff` is not
on PATH in this workspace; the same focused lint passed through `uv`.
Full AWF/GitHub validation is managed by AWF after agent completion.
