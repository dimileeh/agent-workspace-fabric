# Preserved Active Worktree Unknown Validation

Plan reference: `plans/PRESERVED_ACTIVE_WORKTREE_UNKNOWN_PLAN.md`

## Requirement Status

- Classify an unavailable or non-`Path` preserved-active worktree root as `ambiguous`, not `no_work`: Complete.
- Preserve existing `no_work` replacement behavior when a known worktree path is available but the path is missing: Complete.
- Add or update regression coverage proving unavailable path recovery requires operator handling instead of replacement: Complete.
- Keep changes scoped and avoid unrelated refactors: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRESERVED_ACTIVE_WORKTREE_UNKNOWN_PLAN.md`
- `plans/PRESERVED_ACTIVE_WORKTREE_UNKNOWN_VALIDATION.md`

Tests and checks:

- Failed first as expected before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_unavailable_worktree_root or preserved_active_without_usable_work"`
  - Failure: unavailable worktree root still classified as `no_work`.
- Passed after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_unavailable_worktree_root or preserved_active_unknown_worktree_root or preserved_active_without_usable_work"`
  - Result: 8 passed.
- Passed adjacent preserved-active behavior:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_ambiguous_dirty_worktree or preserved_active_git_status_failure or preserved_active_missing_branch_name"`
  - Result: 4 passed.
- Passed static checks:
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - `uv run --python 3.12 --extra dev mypy src/awf`

## Gaps

None.
